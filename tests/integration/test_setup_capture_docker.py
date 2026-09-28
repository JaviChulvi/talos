"""Read-only capture on both actual pinned native runtime images."""

import io
import json
import os
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import docker
import pytest

from backend.app.models import RUNTIME_RELEASES
from backend.app.setups import read_bundle, validate_manifest, write_bundle
from worker.runtime import NATIVE_IMAGES
from worker.setup_capture import capture_setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_native_capture_roundtrip_is_offline_and_leaves_source_unchanged(runtime_kind):
    client = docker.from_env(timeout=90)
    prefix = "talos-capture-test-" + uuid4().hex
    labels = {"io.talos.test": prefix}
    volume = client.volumes.create(name=prefix, labels=labels)
    image = client.images.get(NATIVE_IMAGES[runtime_kind])
    uid = 10000 if runtime_kind == "hermes" else 1000
    source = None
    source_files = {
        "skills/sales/SKILL.md": (
            b"---\nname: sales\ndescription: Qualify leads\n---\nSales guide.\n"
        ),
        "skills/sales/references/qualification.md": b"Qualification questions\n",
        "skills/sales/scripts/example.py": b"raise RuntimeError('capture must not execute this')\n",
        ".env": b"CRM_TOKEN=PRIVATE-TEST-SENTINEL\n",
        "sessions/chat.json": b'{"content":"PRIVATE-TEST-SENTINEL"}',
        "memory/MEMORY.md": b"PRIVATE-TEST-SENTINEL",
        "workspace/private.csv": b"PRIVATE-TEST-SENTINEL",
        "workspace/AGENTS.md": b"PRIVATE-TEST-SENTINEL employee identity",
    }
    if runtime_kind == "hermes":
        # Exercise safe YAML parsing, not a native loader that would hydrate .env.
        source_files["config.yaml"] = b"""terminal:
  cwd: /opt/data/workspace
mcp_servers:
  crm:
    transport: streamable-http
    url: https://crm.example.test/mcp
    headers:
      Authorization: Bearer PRIVATE-TEST-SENTINEL
    tools:
      include: [lookup]
"""
    else:
        # Exercise the image's JSON5 parser with unquoted keys and a trailing comma.
        source_files["openclaw.json"] = b"""{
  agents: {defaults: {workspace: '/home/node/.openclaw/workspace'}},
  mcp: {servers: {crm: {
    transport: 'streamable-http', url: 'https://crm.example.test/mcp',
    headers: {Authorization: 'Bearer PRIVATE-TEST-SENTINEL'},
    toolFilter: {include: ['lookup']},
  }}},
}
"""
    try:
        source = client.containers.create(
            image.id,
            name=prefix + "-source",
            entrypoint=["python3", "-c"],
            command=[
                "import os\n"
                "for parent, dirs, files in os.walk('/state'):\n"
                f" os.chown(parent,{uid},{uid})\n"
                f" for name in files: os.chown(os.path.join(parent,name),{uid},{uid})\n"
            ],
            user="0:0",
            volumes={volume.name: {"bind": "/state", "mode": "rw"}},
            read_only=True,
            network_mode="none",
            labels=labels,
        )
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as handle:
            for path, content in source_files.items():
                entry = tarfile.TarInfo(path)
                entry.size, entry.mode = len(content), 0o600
                handle.addfile(entry, io.BytesIO(content))
        source.put_archive("/state", archive.getvalue())
        source.start()
        assert source.wait(timeout=30)["StatusCode"] == 0
        incarnation = SimpleNamespace(
            image_digest=image.id,
            runtime_release=RUNTIME_RELEASES[runtime_kind],
            container_id=source.id,
            container_name=source.name,
            config_volume=prefix + "-config",
        )
        result = capture_setup(client, volume.name, incarnation, runtime_kind, labels)
        manifest = validate_manifest(result["manifest"], result["files"])
        exported = write_bundle(manifest, result["files"])
        restored, restored_files = read_bundle(exported, publication=True)
        assert restored["assets"] == manifest["assets"]
        assert restored_files == result["files"]
        assert set(restored_files) == {p for p in source_files if p.startswith("skills/")}
        assert "PRIVATE-TEST-SENTINEL" not in json.dumps(result["manifest"])
        assert manifest["connectors"][0]["headers"]["Authorization"] == {
            "slot": "crm",
            "field": "headers_Authorization",
        }
        # Readback via the stopped original container proves source bytes survived.
        stream, _ = source.get_archive("/state")
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r") as handle:
            actual = {
                entry.name.removeprefix("state/"): handle.extractfile(entry).read()
                for entry in handle
                if entry.isfile()
            }
        assert actual == source_files
        assert not client.containers.list(all=True, filters={"name": prefix + "-config-capture"})
    finally:
        if source is not None:
            source.remove(force=True)
        volume.remove()
        client.close()
