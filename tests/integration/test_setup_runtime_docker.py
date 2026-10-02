"""Native skills and MCP discovery in the pinned images, without provider calls."""

import hashlib
import io
import json
import os
import tarfile
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import docker
import pytest

from backend.app.capabilities import compile_permissions
from worker.runtime import (
    NATIVE_IMAGES,
    STATE_PATH,
    RuntimeReadinessError,
    native_config,
    prepare_volumes,
)
from worker.setup_runtime import apply_setup, prepare_setup, verify_setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]

# Minimal complete MCP server. Discovery lists tools; no test invokes a business action.
SERVER = b"""
import json, sys

for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        result = {
            "protocolVersion": m["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif m["method"] == "tools/list":
        result = {
            "tools": [
                {
                    "name": "lookup",
                    "description": "Read fixture data",
                    "inputSchema": {"type": "object", "properties": {}},
                },
                {
                    "name": "forbidden",
                    "description": "Not granted",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]
        }
    elif m["method"] == "ping":
        result = {}
    else:
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": m["id"],
                    "error": {"code": -32601, "message": "Not supported"},
                }
            ),
            flush=True,
        )
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
"""


def put_file(client, image, volume, mount, path, content, uid):
    container = client.containers.create(
        image,
        entrypoint=["true"],
        network_mode="none",
        labels=client.volumes.get(volume).attrs.get("Labels") or {},
        volumes={volume: {"bind": mount, "mode": "rw"}},
    )
    try:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            item = tarfile.TarInfo(path)
            item.size, item.uid, item.gid, item.mode = len(content), uid, uid, 0o600
            archive.addfile(item, io.BytesIO(content))
        container.put_archive(mount, data.getvalue())
    finally:
        container.remove(force=True)


def hold_tmpfs(client, image, volumes, labels):
    """Keep disposable RAM-backed volumes mounted between offline helpers."""
    if os.environ.get("TALOS_TEST_TMPFS_VOLUMES") != "1":
        return None
    for name in volumes:
        client.volumes.create(
            name=name,
            labels=labels,
            driver_opts={"type": "tmpfs", "device": "tmpfs", "o": "size=256m"},
        )
    return client.containers.run(
        image,
        entrypoint=["sleep", "infinity"],
        network_mode="none",
        labels=labels,
        read_only=True,
        detach=True,
        volumes={
            name: {"bind": f"/keep/{index}", "mode": "ro"} for index, name in enumerate(volumes)
        },
    )


@pytest.mark.parametrize("kind", ["openclaw", "hermes"])
def test_setup_reproduces_native_skills_and_mcp_and_preserves_unmanaged_state(kind, monkeypatch):
    client = docker.from_env(timeout=180)
    secret = {"crm": {"token": "synthetic-quote\"-slash\\n-apostrophe'"}}
    prefix = "talos-setup-test-" + uuid4().hex
    labels = {"io.talos.test": prefix}
    if run_id := os.environ.get("TALOS_RELIABILITY_RUN_ID"):
        labels["io.talos.reliability-run"] = run_id
    image = client.images.get(
        os.environ.get("TALOS_TEST_" + kind.upper() + "_IMAGE", NATIVE_IMAGES[kind])
    )
    incarnation = SimpleNamespace(image_digest=image.id, config_volume=prefix + "-config")
    keeper = hold_tmpfs(client, image.id, (prefix, incarnation.config_volume), labels)
    mount, uid = ("/opt/data", 10000) if kind == "hermes" else (STATE_PATH, 1000)
    files = {
        "skills/fixture/SKILL.md": (
            b"---\nname: talos-setup-fixture\ndescription: Read the fixture connector.\n"
            b"---\nUse lookup for fixture data.\n"
        ),
        "connectors/fixture/server.py": SERVER,
        "skills/fixture/scripts/run.sh": b"#!/bin/sh\nprintf executable-fixture\n",
        "skills/disabled/SKILL.md": (
            b"---\nname: talos-disabled-fixture\ndescription: Disabled.\n---\nDo not load.\n"
        ),
    }
    manifest = {
        "instructions": "Use the fixture skill.",
        "targets": [
            {
                "runtime_kind": kind,
                "runtime_release": (
                    "hermes-0.21.5"
                    if kind == "hermes"
                    else "openclaw-"
                    + image.attrs["Config"]["Labels"]["org.opencontainers.image.version"]
                ),
                "architecture": image.attrs["Architecture"],
            }
        ],
        "skills": [
            {
                "id": "fixture",
                "name": "talos-setup-fixture",
                "path": "skills/fixture",
                "enabled": True,
            },
            {
                "id": "disabled",
                "name": "talos-disabled-fixture",
                "path": "skills/disabled",
                "enabled": False,
            },
        ],
        "connection_slots": [{"id": "crm", "label": "CRM", "fields": ["token"]}],
        "connectors": [
            {
                "id": "fixture",
                "transport": "stdio",
                "runner": "python3",
                "entrypoint": "connectors/fixture/server.py",
                "tools": ["lookup"],
                "env": {"FIXTURE_TOKEN": {"slot": "crm", "field": "token"}},
                "enabled": True,
            }
        ],
        "assets": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        "executables": ["skills/fixture/scripts/run.sh"],
    }
    artifact_hash = hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    app = {
        "setup": {"artifact_hash": artifact_hash, "manifest": manifest},
        "permissions": compile_permissions([], kind),
        "connector_grants": ["fixture"],
    }
    monkeypatch.setattr("backend.app.setups.load_bundle", lambda _: (manifest, files))
    try:
        prepare_volumes(
            client,
            prefix,
            incarnation.config_volume,
            native_config(kind, "synthetic-hash"),
            labels,
            native=True,
            runtime_kind=kind,
        )
        put_file(
            client, image.id, prefix, mount, "workspace/AGENTS.md", b"Personal instructions.\n", uid
        )
        put_file(
            client, image.id, prefix, mount, "workspace/conversation.txt", b"Keep this history", uid
        )
        # Compatibility and shadow conflicts fail in read-only preflight.
        incompatible = deepcopy(app)
        incompatible["setup"]["manifest"]["targets"][0]["architecture"] = "unsupported"
        with monkeypatch.context() as patch:
            patch.setattr(
                "backend.app.setups.load_bundle",
                lambda _: (incompatible["setup"]["manifest"], files),
            )
            with pytest.raises(RuntimeReadinessError, match="architecture"):
                prepare_setup(
                    client, prefix, incarnation, kind, incompatible, labels, secrets=secret
                )
        put_file(
            client,
            image.id,
            prefix,
            mount,
            "skills/shadow/SKILL.md",
            files["skills/fixture/SKILL.md"],
            uid,
        )
        with pytest.raises(RuntimeReadinessError, match="shadows"):
            prepare_setup(client, prefix, incarnation, kind, app, labels, secrets=secret)
        # Replace the conflicting skill with a different personal skill; it must survive.
        put_file(
            client,
            image.id,
            prefix,
            mount,
            "skills/shadow/SKILL.md",
            b"---\nname: personal-skill\ndescription: Personal.\n---\nKeep.\n",
            uid,
        )
        prepared = prepare_setup(client, prefix, incarnation, kind, app, labels, secrets=secret)
        receipt = apply_setup(
            client, prefix, incarnation, kind, app, labels, prepared=prepared, secrets=secret
        )
        assert (
            verify_setup(client, prefix, incarnation, kind, app, labels)["fingerprint"]
            == receipt["fingerprint"]
        )
        assert (
            apply_setup(client, prefix, incarnation, kind, app, labels, secrets=secret)[
                "fingerprint"
            ]
            == receipt["fingerprint"]
        )
        script = f"{mount}/.talos/setups/{artifact_hash}/enabled-skills/fixture/scripts/run.sh"
        assert client.containers.run(
            image.id, entrypoint=[script], network_mode="none", user=f"{uid}:{uid}",
            volumes={prefix: {"bind": mount, "mode": "ro"}}, remove=True,
        ) == b"executable-fixture"
        # Missing owner execute permission must fail even if group execution remains.
        for permissions in ("600", "610"):
            client.containers.run(
                image.id, entrypoint=["chmod", permissions, script], network_mode="none",
                user=f"{uid}:{uid}", volumes={prefix: {"bind": mount, "mode": "rw"}}, remove=True,
            )
            with pytest.raises(RuntimeReadinessError, match="edited"):
                verify_setup(client, prefix, incarnation, kind, app, labels, discover=False)
        client.containers.run(
            image.id, entrypoint=["chmod", "700", script], network_mode="none",
            user=f"{uid}:{uid}", volumes={prefix: {"bind": mount, "mode": "rw"}}, remove=True,
        )
        # Crashes at each publication boundary must leave a resumable pending receipt.
        import worker.setup_runtime as adapter

        for boundary in (
            'atomic(root / ".env", env)',
            "save_config(path, updated)",
            "atomic(receipt_path, json.dumps(target, sort_keys=True).encode())",
        ):
            assert boundary in adapter._HELPER
            with monkeypatch.context() as patch:
                patch.setattr(
                    adapter,
                    "_HELPER",
                    adapter._HELPER.replace(boundary, boundary + '; fail("native")'),
                )
                with pytest.raises(RuntimeReadinessError):
                    apply_setup(client, prefix, incarnation, kind, app, labels, secrets=secret)
            with pytest.raises(RuntimeReadinessError, match="receipt"):
                verify_setup(client, prefix, incarnation, kind, app, labels)
            apply_setup(client, prefix, incarnation, kind, app, labels, secrets=secret)
        assert secret["crm"]["token"] not in json.dumps(receipt)
        # Re-application must preserve personal instructions and history.
        reader = client.containers.create(
            image.id,
            entrypoint=["true"],
            network_mode="none",
            volumes={prefix: {"bind": mount, "mode": "ro"}},
        )
        try:
            texts = []
            for path in ("workspace/AGENTS.md", "workspace/conversation.txt"):
                stream, _ = reader.get_archive(mount + "/" + path)
                with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
                    texts.append(archive.extractfile(archive.getmembers()[0]).read().decode())
            doc, history = texts
        finally:
            reader.remove(force=True)
        assert doc.count("TALOS SETUP BEGIN") == 1
        assert doc.startswith("Personal instructions.") and history == "Keep this history"
        assert (
            verify_setup(client, prefix, incarnation, kind, app, labels, discover=False)[
                "fingerprint"
            ]
            == receipt["fingerprint"]
        )
        # Reject user modification of an owned file, including container adoption.
        put_file(
            client,
            image.id,
            prefix,
            mount,
            ".talos/setups/" + artifact_hash + "/connectors/fixture/server.py",
            b"edited",
            uid,
        )
        with pytest.raises(RuntimeReadinessError, match="edited"):
            verify_setup(client, prefix, incarnation, kind, app, labels)
        with pytest.raises(RuntimeReadinessError, match="edited"):
            verify_setup(client, prefix, incarnation, kind, app, labels, discover=False)
        put_file(
            client,
            image.id,
            prefix,
            mount,
            ".talos/setups/" + artifact_hash + "/connectors/fixture/server.py",
            SERVER,
            uid,
        )
        # An explicit replacement can supersede an interrupted application too.
        with monkeypatch.context() as patch:
            patch.setattr(
                adapter,
                "_HELPER",
                adapter._HELPER.replace(
                    "save_config(path, updated)", 'save_config(path, updated); fail("native")'
                ),
            )
            with pytest.raises(RuntimeReadinessError):
                apply_setup(client, prefix, incarnation, kind, app, labels, secrets=secret)
        # Removing the setup clears its native entries and instruction section.
        empty = {
            "setup": None,
            "permissions": compile_permissions([], kind),
            "connector_grants": [],
        }
        apply_setup(client, prefix, incarnation, kind, empty, labels)
        verify_setup(client, prefix, incarnation, kind, empty, labels)
    finally:
        if keeper:
            keeper.remove(force=True)
        for name in (prefix, incarnation.config_volume):
            client.volumes.get(name).remove()
        client.close()
