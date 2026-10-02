"""Pinned-image setup upgrade/rollback checks; no inference or external services."""

import hashlib
import io
import json
import os
import sqlite3
import tarfile
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import docker
import pytest

from backend.app.capabilities import compile_permissions
from tests.integration.test_setup_runtime_docker import hold_tmpfs, put_file
from worker import setup_runtime as adapter
from worker.runtime import (
    NATIVE_IMAGES,
    STATE_PATH,
    RuntimeReadinessError,
    prepare_volumes,
    runtime_config,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("TALOS_TEST_DOCKER") != "1", reason="Set TALOS_TEST_DOCKER=1"
    ),
]


@pytest.fixture
def installation(monkeypatch, tmp_path):
    client = docker.from_env(timeout=180)
    image = client.images.get(
        os.environ.get("TALOS_TEST_OPENCLAW_IMAGE", NATIVE_IMAGES["openclaw"])
    )
    state = "talos-rollback-proof-" + uuid4().hex
    labels = {"io.talos.reliability": state}
    if run_id := os.environ.get("TALOS_RELIABILITY_RUN_ID"):
        labels["io.talos.reliability-run"] = run_id
    incarnation = SimpleNamespace(image_digest=image.id, config_volume=state + "-config")
    keeper = hold_tmpfs(client, image.id, (state, incarnation.config_volume), labels)
    bundles = {}
    prepare_volumes(
        client,
        state,
        incarnation.config_volume,
        runtime_config("synthetic-control", "synthetic-provider", "http://unused"),
        labels,
        native=True,
    )
    memory = tmp_path / "memory.sqlite"
    with sqlite3.connect(memory) as connection:
        connection.executescript(
            "PRAGMA user_version=1; CREATE TABLE memories(value TEXT); "
            "INSERT INTO memories VALUES('employee-private-memory');"
        )
    put_file(client, image.id, state, STATE_PATH, "memory/proof.sqlite", memory.read_bytes(), 1000)
    put_file(
        client, image.id, state, STATE_PATH, "workspace/conversation.txt", b"private-history", 1000
    )
    put_file(
        client,
        image.id,
        state,
        STATE_PATH,
        "workspace/AGENTS.md",
        b"Personal instructions.\n",
        1000,
    )
    monkeypatch.setattr("backend.app.setups.load_bundle", lambda digest: bundles[digest])

    def application(version):
        payload = (
            "---\nname: talos-rollback-proof\ndescription: Local rollback proof.\n---\n"
            + f"Instructions for version {version}.\n"
        ).encode()
        files = {"skills/proof/SKILL.md": payload}
        manifest = {
            "schema_version": 1,
            "targets": [
                {
                    "runtime_kind": "openclaw",
                    "runtime_release": "openclaw-"
                    + image.labels["org.opencontainers.image.version"],
                    "architecture": image.attrs["Architecture"],
                }
            ],
            "instructions": f"Managed instructions {version}.",
            "skills": [{"id": "proof", "name": "talos-rollback-proof", "path": "skills/proof"}],
            "connectors": [],
            "connection_slots": [],
            "assets": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        }
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        bundles[digest] = (manifest, files)
        return {
            "setup": {"artifact_hash": digest, "manifest": manifest},
            "permissions": compile_permissions(
                ["workspace_files"] if version == 1 else [], "openclaw"
            ),
            "connector_grants": [],
        }

    def read(path):
        container = client.containers.create(
            image.id,
            entrypoint=["true"],
            network_mode="none",
            labels=labels,
            volumes={state: {"bind": STATE_PATH, "mode": "ro"}},
        )
        try:
            stream, _ = container.get_archive(STATE_PATH + "/" + path)
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
                return archive.extractfile(archive.getmembers()[0]).read()
        finally:
            container.remove(force=True)

    def write(path, value):
        put_file(client, image.id, state, STATE_PATH, path, json.dumps(value).encode(), 1000)

    def apply(app):
        return adapter.apply_setup(client, state, incarnation, "openclaw", app, labels)

    def inspect(app):
        return adapter.verify_setup(
            client, state, incarnation, "openclaw", app, labels, discover=False
        )

    try:
        yield SimpleNamespace(
            client=client,
            state=state,
            incarnation=incarnation,
            labels=labels,
            image=image,
            application=application,
            read=read,
            write=write,
            apply=apply,
            inspect=inspect,
        )
    finally:
        if keeper:
            keeper.remove(force=True)
        for name in (state, incarnation.config_volume):
            client.volumes.get(name).remove()
        client.close()


@pytest.mark.parametrize(
    "boundary",
    [
        None,
        'atomic(root / ".env", env)',
        "save_config(path, updated)",
        "atomic(receipt_path, json.dumps(target, sort_keys=True).encode())",
    ],
)
def test_upgrade_and_interrupted_upgrade_can_roll_back_without_changing_memory(
    installation, monkeypatch, boundary
):
    proof = installation
    old, new = proof.application(1), proof.application(2)
    receipt = proof.apply(old)
    assert receipt["schema_version"] == 1
    assert receipt["runtime"]["id"] == proof.image.id
    assert receipt["policy"]["allow"] == ["apply_patch", "edit", "read", "write"]
    original = {
        path: proof.read(path)
        for path in (
            "memory/proof.sqlite",
            "workspace/conversation.txt",
            "workspace/AGENTS.md",
            "openclaw.json",
        )
    }
    if boundary:
        assert boundary in adapter._HELPER
        with monkeypatch.context() as patch:
            # Exit the actual helper process without its exception/finally handlers.
            patch.setattr(
                adapter, "_HELPER", adapter._HELPER.replace(boundary, boundary + "; os._exit(91)")
            )
            with pytest.raises(RuntimeReadinessError):
                proof.apply(new)
        with pytest.raises(RuntimeReadinessError) as error:
            proof.inspect(old)
        assert error.value.code in {"receipt", "edited"}
    else:
        proof.apply(new)
        assert proof.inspect(new)["policy"] == {"allow": ["read"], "deny": []}
    # Explicitly reselect the old immutable setup; do not retry an external action.
    proof.apply(old)
    assert proof.inspect(old)["fingerprint"] == receipt["fingerprint"]
    for path, data in original.items():
        assert proof.read(path) == data, path


@pytest.mark.parametrize("location", ["receipt", "pending", "pending_previous", "pending_target"])
def test_unknown_receipt_schema_is_rejected_before_state_mutation(installation, location):
    proof = installation
    app = proof.application(1)
    receipt = proof.apply(app)
    original = {
        path: proof.read(path)
        for path in (
            "openclaw.json",
            "workspace/AGENTS.md",
            "memory/proof.sqlite",
        )
    }
    if location == "receipt":
        proof.write(".talos/setup-receipt.json", {**receipt, "schema_version": 99})
    else:
        pending = {"schema_version": 1, "previous": deepcopy(receipt), "target": deepcopy(receipt)}
        key = {"pending_previous": "previous", "pending_target": "target"}.get(location)
        (pending[key] if key else pending)["schema_version"] = 99
        proof.write(".talos/setup-pending.json", pending)
    for operation in (proof.apply, proof.inspect):
        with pytest.raises(RuntimeReadinessError) as error:
            operation(app)
        assert error.value.code == "receipt_version"
    for path, data in original.items():
        assert proof.read(path) == data


def test_legacy_receipt_upgrade_and_runtime_pin_mismatch(installation):
    proof = installation
    app = proof.application(1)
    receipt = proof.apply(app)
    legacy = {
        key: value
        for key, value in receipt.items()
        if key not in {"schema_version", "runtime", "policy"}
    }
    proof.write(".talos/setup-receipt.json", legacy)
    assert proof.inspect(app)["fingerprint"] == receipt["fingerprint"]
    proof.apply(app)
    pinned = proof.inspect(app)
    assert pinned["schema_version"] == 1 and pinned["runtime"]["id"] == proof.image.id
    proof.write(
        ".talos/setup-receipt.json",
        {**pinned, "runtime": {**pinned["runtime"], "id": "other-image"}},
    )
    with pytest.raises(RuntimeReadinessError) as error:
        proof.inspect(app)
    assert error.value.code == "compatibility"


def test_incompatible_upgrade_preserves_current_setup(installation, monkeypatch):
    proof = installation
    old, new = proof.application(1), proof.application(2)
    receipt = proof.apply(old)
    incompatible = deepcopy(new)
    incompatible["setup"]["manifest"]["targets"][0]["runtime_release"] = "openclaw-unsupported"
    with monkeypatch.context() as patch:
        patch.setattr(
            "backend.app.setups.load_bundle", lambda _: (incompatible["setup"]["manifest"], {})
        )
        with pytest.raises(RuntimeReadinessError) as error:
            adapter.prepare_setup(
                proof.client,
                proof.state,
                proof.incarnation,
                "openclaw",
                incompatible,
                proof.labels,
            )
        assert error.value.code == "compatibility"
    assert proof.inspect(old)["fingerprint"] == receipt["fingerprint"]
