"""Hosted MCP protocol discovery, tool filtering, and credentials in both pinned runtimes."""

import hashlib
import json
import os
import time
from types import SimpleNamespace
from uuid import uuid4

import docker
import pytest
from docker.errors import NotFound

from backend.app.capabilities import compile_permissions
from worker.runtime import NATIVE_IMAGES, RuntimeReadinessError, native_config, prepare_volumes
from worker.setup_runtime import apply_setup, prepare_setup, verify_setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]

# Synthetic data only. Include punctuation that the two native dotenv parsers treat differently.
TOKEN = 'Bearer fixture-"quoted"\\token\''
# The proxy handles this public-address URL entirely in a Docker network with no external egress.
URL = "http://93.184.216.34/mcp"
SERVER = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
TOKEN = "Bearer fixture-\"quoted\"\\token'"
events = []
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_CONNECT(self):
        if self.path != '93.184.216.34:80': return self.reply(403)
        self.reply(200)
        self.close_connection = False
    def log_message(self, *args): pass
    def reply(self, status, body=None):
        data = json.dumps(body).encode() if body is not None else b''
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        if self.path == '/audit': return self.reply(200, events)
        self.reply(405)
    def do_DELETE(self): self.reply(200)
    def do_POST(self):
        try: message = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        except Exception: return self.reply(400)
        authorized = self.headers.get('Authorization') == TOKEN
        events.append({'path': self.path, 'method': message.get('method'),
                       'authorized': authorized})
        if urlsplit(self.path).path != '/mcp': return self.reply(404)
        if not authorized: return self.reply(401)
        if 'id' not in message: return self.reply(202)
        if message['method'] == 'initialize':
            result = {'protocolVersion': message['params']['protocolVersion'],
                      'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'hosted-fixture', 'version': '1'}}
        elif message['method'] == 'tools/list':
            result = {'tools': [
                {'name': 'allowed', 'description': 'Granted discovery fixture',
                 'inputSchema': {'type': 'object', 'properties': {}}},
                {'name': 'forbidden', 'description': 'Excluded discovery fixture',
                 'inputSchema': {'type': 'object', 'properties': {}}},
            ]}
        elif message['method'] == 'ping': result = {}
        else:
            return self.reply(200, {'jsonrpc': '2.0', 'id': message['id'],
                                   'error': {'code': -32601,
                                             'message': 'Discovery only'}})
        self.reply(200, {'jsonrpc': '2.0', 'id': message['id'], 'result': result})
ThreadingHTTPServer(('0.0.0.0', 3128), Handler).serve_forever()
"""


def audit(proxy):
    result = proxy.exec_run(
        [
            "python3",
            "-c",
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:3128/audit').read().decode())",
        ]
    )
    if result.exit_code:
        return None
    return json.loads(result.output)


@pytest.mark.parametrize("kind", ["openclaw", "hermes"])
def test_hosted_mcp_discovers_only_granted_tools_with_exact_bound_credentials(kind, monkeypatch):
    client = docker.from_env(timeout=180)
    prefix = "talos-hosted-test-" + uuid4().hex
    labels = {"io.talos.test": prefix}
    image = client.images.get(NATIVE_IMAGES[kind])
    incarnation = SimpleNamespace(image_digest=image.id, config_volume=prefix + "-config")
    network = client.networks.create(prefix, internal=True, labels=labels)
    proxy = None
    try:
        proxy = client.containers.create(
            NATIVE_IMAGES["hermes"],
            name=prefix + "-proxy",
            entrypoint=["python3", "-c"],
            command=[SERVER],
            network=network.name,
            networking_config={
                network.name: client.api.create_endpoint_config(aliases=["talos-egress"])
            },
            read_only=True,
            user="10000:10000",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            labels=labels,
            mem_limit="128m",
            pids_limit=32,
        )
        proxy.start()
        deadline = time.monotonic() + 15
        while audit(proxy) is None:
            assert time.monotonic() < deadline, "Fixture HTTP proxy failed to start"
            time.sleep(0.1)
        prepare_volumes(
            client,
            prefix,
            incarnation.config_volume,
            native_config(kind, "fixture-only-hash"),
            labels,
            native=True,
            runtime_kind=kind,
        )
        release = (
            "hermes-0.21.5"
            if kind == "hermes"
            else ("openclaw-" + image.attrs["Config"]["Labels"]["org.opencontainers.image.version"])
        )
        manifest = {
            "instructions": "",
            "skills": [],
            "assets": {},
            "targets": [
                {
                    "runtime_kind": kind,
                    "runtime_release": release,
                    "architecture": image.attrs["Architecture"],
                }
            ],
            "connectors": [
                {
                    "id": "hosted",
                    "transport": "streamable-http",
                    "url": URL,
                    "tools": ["allowed"],
                    "enabled": True,
                    "headers": {"Authorization": {"slot": "crm", "field": "token"}},
                },
                {
                    "id": "denied",
                    "transport": "streamable-http",
                    "url": URL + "/denied",
                    "tools": ["forbidden"],
                    "enabled": True,
                },
            ],
            "connection_slots": [{"id": "crm", "label": "Fixture account", "fields": ["token"]}],
        }
        artifact_hash = hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
        application = {
            "setup": {"artifact_hash": artifact_hash, "manifest": manifest},
            "permissions": compile_permissions([], kind),
            "connector_grants": ["hosted"],
            "connections": {
                "crm": {
                    "connection_id": str(uuid4()),
                    "version_id": str(uuid4()),
                    "fields": ["token"],
                }
            },
        }
        secrets = {"crm": {"token": TOKEN}}
        monkeypatch.setattr("backend.app.setups.load_bundle", lambda _: (manifest, {}))
        prepared = prepare_setup(
            client, prefix, incarnation, kind, application, labels, secrets=secrets
        )
        installed = apply_setup(
            client,
            prefix,
            incarnation,
            kind,
            application,
            labels,
            prepared=prepared,
            secrets=secrets,
        )
        verified = verify_setup(
            client, prefix, incarnation, kind, application, labels, network=network.name
        )
        assert verified["fingerprint"] == installed["fingerprint"]
        events = audit(proxy)
        assert any(event["method"] == "initialize" for event in events)
        assert any(event["method"] == "tools/list" for event in events)
        assert all(event["authorized"] for event in events)
        assert all(event["path"] in {URL, "/mcp"} for event in events)
        assert not any(event["method"] == "tools/call" for event in events)
        # An unavailable remote account must fail readiness; the receipt alone is insufficient.
        proxy.stop(timeout=1)
        with pytest.raises(RuntimeReadinessError):
            verify_setup(
                client, prefix, incarnation, kind, application, labels, network=network.name
            )
    finally:
        if proxy is not None:
            proxy.remove(force=True)
        for name in (prefix, incarnation.config_volume):
            try:
                client.volumes.get(name).remove()
            except NotFound:
                pass
        network.remove()
        client.close()
