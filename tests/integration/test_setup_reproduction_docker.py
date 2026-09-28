"""Capture and reproduce portable setups within and across both pinned runtimes."""

import hashlib
import io
import json
import os
import tarfile
from types import SimpleNamespace
from uuid import uuid4

import docker
import pytest

from backend.app.capabilities import compile_permissions
from backend.app.models import RUNTIME_RELEASES
from backend.app.setups import (
    load_bundle,
    read_bundle,
    store_bundle,
    validate_manifest,
    write_bundle,
)
from worker.runtime import (
    NATIVE_IMAGES,
    STATE_PATH,
    RuntimeReadinessError,
    native_config,
    prepare_volumes,
)
from worker.setup_capture import capture_setup
from worker.setup_runtime import apply_setup, prepare_setup, verify_setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]

# Only discovery is implemented. Credentials must arrive through native env hydration.
# A business tool call fails instead of performing any action.
SERVER = b"""import json,os,sys
assert os.environ.get('CRM_API_KEY','').startswith('synthetic-reproduction-')
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    method=request['method']
    if method=='initialize':
        result={'protocolVersion':request['params']['protocolVersion'],
                'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
    elif method=='tools/list':
        result={'tools':[{'name':name,'description':'Fixture discovery only',
                         'inputSchema':{'type':'object','properties':{}}}
                        for name in ('lookup','forbidden')]}
    elif method=='ping': result={}
    elif method=='tools/call': raise SystemExit('Business actions are prohibited in this proof')
    else:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],
                          'error':{'code':-32601,'message':'Not supported'}}),flush=True)
        continue
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
"""


def read_state_file(client, image_id, state, mount, relative):
    reader = client.containers.create(
        image_id,
        entrypoint=["true"],
        network_mode="none",
        read_only=True,
        volumes={state: {"bind": mount, "mode": "ro"}},
    )
    try:
        stream, _ = reader.get_archive(mount + "/" + relative)
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
            return archive.extractfile(archive.getmembers()[0]).read()
    finally:
        reader.remove(force=True)


@pytest.mark.parametrize(
    ("source_kind", "kind"),
    [
        ("openclaw", "openclaw"),
        ("hermes", "hermes"),
        ("hermes", "openclaw"),
        ("openclaw", "hermes"),
    ],
)
def test_captured_setup_reproduces_with_new_accounts_and_in_fresh_installation(
    source_kind,
    kind,
    monkeypatch,
    tmp_path,
):
    client = docker.from_env(timeout=180)
    prefix = "talos-reproduction-test-" + uuid4().hex
    labels = {"io.talos.test": prefix}
    images = {runtime: client.images.get(NATIVE_IMAGES[runtime]) for runtime in {source_kind, kind}}
    volumes = []
    create_calls = []
    original_create = client.containers.create

    def tracked_create(*args, **kwargs):
        create_calls.append((args, kwargs))
        return original_create(*args, **kwargs)

    monkeypatch.setattr(client.containers, "create", tracked_create)
    registry = SimpleNamespace(setup_artifacts_dir=tmp_path / "installation-a")
    monkeypatch.setattr("backend.app.setups.get_settings", lambda: registry)
    try:
        targets = {}
        for runtime, image in images.items():
            interpreters = json.loads(
                client.containers.run(
                    image.id,
                    entrypoint=["python3", "-c"],
                    command=[
                        "import json,subprocess,sys;print(json.dumps({"
                        "'python_version':str(sys.version_info.major)+'.'+str(sys.version_info.minor),"
                        "'node_major':int(subprocess.check_output(['node','-p',"
                        "'process.versions.node.split(\".\")[0]']))}))"
                    ],
                    network_mode="none",
                    read_only=True,
                    remove=True,
                )
            )
            targets[runtime] = {
                "runtime_kind": runtime,
                "runtime_release": RUNTIME_RELEASES[runtime],
                "architecture": image.attrs["Architecture"],
                **interpreters,
            }
        files = {
            "skills/sales/SKILL.md": (
                b"---\nname: talos-reproduction-sales\ndescription: Qualify sales leads.\n"
                b"---\nUse lookup to qualify leads and consult the rubric.\n"
            ),
            "skills/sales/references/rubric.md": b"Ask about the account's needs.\n",
            "connectors/crm/server.py": SERVER,
        }
        manifest = {
            "schema_version": 1,
            "instructions": "Use the sales skill and CRM connector to qualify leads.",
            "targets": [targets[source_kind]],
            "skills": [
                {
                    "id": "sales",
                    "name": "talos-reproduction-sales",
                    "path": "skills/sales",
                    "enabled": True,
                }
            ],
            "connectors": [
                {
                    "id": "crm",
                    "name": "Fixture CRM",
                    "transport": "stdio",
                    "runner": "python3",
                    "entrypoint": "connectors/crm/server.py",
                    "tools": ["lookup"],
                    "enabled": True,
                    "env": {"CRM_API_KEY": {"slot": "crm", "field": "token"}},
                    "provenance": "Self-contained Python stdlib fixture version 1",
                }
            ],
            "connection_slots": [{"id": "crm", "label": "CRM account", "fields": ["token"]}],
            "assets": {path: hashlib.sha256(data).hexdigest() for path, data in files.items()},
            "unresolved": [],
        }
        manifest = validate_manifest(manifest, files)
        artifact_hash = store_bundle(write_bundle(manifest, files))

        def new_agent(suffix, runtime_kind=kind):
            image = images[runtime_kind]
            state = prefix + "-" + suffix
            incarnation = SimpleNamespace(
                image_digest=image.id,
                runtime_release=RUNTIME_RELEASES[runtime_kind],
                config_volume=state + "-config",
                container_id=None,
                container_name=None,
            )
            # Each target has independent native state and configuration volumes.
            volumes.extend([state, incarnation.config_volume])
            prepare_volumes(
                client,
                state,
                incarnation.config_volume,
                native_config(runtime_kind, "synthetic-hash"),
                labels,
                native=True,
                runtime_kind=runtime_kind,
            )
            return state, incarnation

        def apply_and_verify(state, incarnation, digest, account, runtime_kind=kind):
            stored, _ = load_bundle(digest)
            application = {
                "setup": {"artifact_hash": digest, "manifest": stored},
                "permissions": compile_permissions([], runtime_kind),
                "connector_grants": ["crm"],
                "connections": {
                    "crm": {
                        "connection_id": str(uuid4()),
                        "version_id": str(uuid4()),
                        "fields": ["token"],
                    }
                },
            }
            secrets = {"crm": {"token": "synthetic-reproduction-" + account}}
            prepared = prepare_setup(
                client,
                state,
                incarnation,
                runtime_kind,
                application,
                labels,
                secrets=secrets,
            )
            installed = apply_setup(
                client,
                state,
                incarnation,
                runtime_kind,
                application,
                labels,
                prepared=prepared,
                secrets=secrets,
            )
            # Native skill lookup plus MCP tools/list verify actual runtime discovery.
            verified = verify_setup(client, state, incarnation, runtime_kind, application, labels)
            assert verified["fingerprint"] == installed["fingerprint"]
            assert verified["manifest"]["assets"] == manifest["assets"]
            mount = "/opt/data" if runtime_kind == "hermes" else STATE_PATH
            env = read_state_file(client, images[runtime_kind].id, state, mount, ".env")
            assert secrets["crm"]["token"].encode() in env
            return application, env

        source, source_incarnation = new_agent("reference", source_kind)
        source_app, source_env = apply_and_verify(
            source, source_incarnation, artifact_hash, "source", source_kind
        )
        source_mount = "/opt/data" if source_kind == "hermes" else STATE_PATH
        source_files = [
            ".talos/setup-receipt.json",
            "config.yaml" if source_kind == "hermes" else "openclaw.json",
            ".env",
            "workspace/AGENTS.md",
        ]
        source_before = {
            path: read_state_file(client, images[source_kind].id, source, source_mount, path)
            for path in source_files
        }
        captured = capture_setup(client, source, source_incarnation, source_kind, labels)
        assert captured["manifest"]["unresolved"] == []
        assert captured["manifest"]["assets"] == manifest["assets"]
        assert captured["files"] == files
        assert captured["manifest"]["instructions"] == manifest["instructions"]
        assert captured["manifest"]["connection_slots"] == manifest["connection_slots"]
        assert all(c["source"] == "talos-managed" for c in captured["metadata"]["candidates"])
        reviewed = validate_manifest(captured["manifest"], captured["files"])
        second, second_incarnation = new_agent("second")
        if source_kind != kind:
            # A captured source-only declaration must not bypass compatibility.
            source_only_hash = store_bundle(write_bundle(reviewed, captured["files"]))
            source_only_application = {
                **source_app,
                "setup": {"artifact_hash": source_only_hash, "manifest": reviewed},
                "permissions": compile_permissions([], kind),
            }
            with pytest.raises(RuntimeReadinessError, match="runtime release"):
                prepare_setup(
                    client,
                    second,
                    second_incarnation,
                    kind,
                    source_only_application,
                    labels,
                    secrets={"crm": {"token": "synthetic-reproduction-second"}},
                )
            # Administrators explicitly add support using measured destination
            # interpreter versions; the captured assets remain byte-identical.
            reviewed["targets"].append(targets[kind])
            reviewed = validate_manifest(reviewed, captured["files"])
            assert reviewed["assets"] == manifest["assets"]
        exported = write_bundle(reviewed, captured["files"])
        capture_hash = store_bundle(exported)

        second_app, second_env = apply_and_verify(
            second, second_incarnation, capture_hash, "second"
        )
        assert source_env != second_env
        assert source_app["connections"] != second_app["connections"]
        assert b"synthetic-reproduction-source" not in second_env
        # Export contains only the recipe, never account identities or credentials.
        imported_manifest, imported_files = read_bundle(exported, publication=True)
        portable_bytes = json.dumps(imported_manifest).encode() + b"".join(imported_files.values())
        for app in (source_app, second_app):
            for binding in app["connections"].values():
                assert binding["connection_id"].encode() not in portable_bytes
                assert binding["version_id"].encode() not in portable_bytes
        assert b"synthetic-reproduction-source" not in portable_bytes
        assert b"synthetic-reproduction-second" not in portable_bytes

        # A new artifact store represents a separate Talos installation: only the
        # exported ZIP is copied, then a different local account is bound.
        registry.setup_artifacts_dir = tmp_path / "installation-b"
        imported_hash = store_bundle(exported)
        assert imported_hash == capture_hash
        assert load_bundle(imported_hash)[1] == files
        fresh, fresh_incarnation = new_agent("fresh-installation")
        fresh_app, fresh_env = apply_and_verify(
            fresh,
            fresh_incarnation,
            imported_hash,
            "fresh-installation",
        )
        assert fresh_app["connections"] not in (
            source_app["connections"],
            second_app["connections"],
        )
        assert b"synthetic-reproduction-source" not in fresh_env
        assert b"synthetic-reproduction-second" not in fresh_env
        recaptured = capture_setup(client, fresh, fresh_incarnation, kind, labels)
        assert recaptured["manifest"]["assets"] == manifest["assets"]
        assert recaptured["files"] == files
        assert recaptured["manifest"]["unresolved"] == []
        source_after = {
            path: read_state_file(client, images[source_kind].id, source, source_mount, path)
            for path in source_files
        }
        assert source_after == source_before
        # Agent accounts traveled through upload/stdin, never Docker command args.
        docker_configuration = json.dumps(create_calls, default=str)
        for account in ("source", "second", "fresh-installation"):
            assert "synthetic-reproduction-" + account not in docker_configuration
    finally:
        for volume in reversed(volumes):
            try:
                client.volumes.get(volume).remove()
            except docker.errors.NotFound:
                pass
        client.close()
