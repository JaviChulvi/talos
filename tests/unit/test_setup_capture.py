"""Capture inventory, portable round trips, and offline helper ownership guarantees."""

import base64
import hashlib
import io
import json
import stat
import subprocess
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from docker.errors import NotFound

from backend.app.models import RUNTIME_RELEASES
from backend.app.setups import read_bundle, validate_manifest, write_bundle
from worker.setup_capture import SetupCaptureError, capture_setup, inspect_state


def state(tmp_path, runtime_kind="openclaw", **config):
    filename = "config.yaml" if runtime_kind == "hermes" else "openclaw.json"
    (tmp_path / filename).write_text(json.dumps(config))
    return tmp_path


def put(root, path, content):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def inspect(root, runtime_kind="openclaw"):
    result = inspect_state(root, runtime_kind, RUNTIME_RELEASES[runtime_kind], "arm64")
    result["files"] = {path: base64.b64decode(data) for path, data in result["files"].items()}
    return result


@pytest.fixture(autouse=True)
def pinned_node(monkeypatch):
    original = subprocess.run

    def run(command, **kwargs):
        if command == ["node", "--version"]:
            return SimpleNamespace(stdout=b"v24.19.0\n")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_capture_preserves_complete_skills_and_source_without_private_state(tmp_path, runtime_kind):
    root = state(tmp_path, runtime_kind)
    put(root, "skills/sales/SKILL.md", b"---\nname: sales\n---\nQualify incoming leads.")
    put(root, "skills/sales/scripts/qualify.py", b"raise RuntimeError('must never run')\n").chmod(
        0o751
    )
    put(root, "skills/sales/references/process.md", b"Our qualification rubric\n")
    for path in (
        ".env",
        "auth.json",
        "sessions/chat.json",
        "memory/MEMORY.md",
        "workspace/client.csv",
    ):
        put(root, path, b"PRIVATE-SENTINEL")
    put(root, "workspace/AGENTS.md", b"User private identity PRIVATE-SENTINEL")
    before = {
        p.relative_to(root): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in root.rglob("*")
        if p.is_file()
    }

    result = inspect(root, runtime_kind)

    assert result["manifest"]["instructions"] == ""
    assert result["metadata"]["instructions_review_required"] is True
    assert set(result["files"]) == {
        "skills/sales/SKILL.md",
        "skills/sales/scripts/qualify.py",
        "skills/sales/references/process.md",
    }
    assert b"PRIVATE-SENTINEL" not in write_bundle(result["manifest"], result["files"])
    assert before == {
        p.relative_to(root): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in root.rglob("*")
        if p.is_file()
    }
    assert result["manifest"]["executables"] == ["skills/sales/scripts/qualify.py"]
    manifest = validate_manifest(result["manifest"], result["files"])
    exported = write_bundle(manifest, result["files"])
    imported, files = read_bundle(exported, publication=True)
    assert files == result["files"]
    assert imported["assets"] == manifest["assets"]
    assert imported["executables"] == manifest["executables"]
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        script_mode = archive.getinfo("skills/sales/scripts/qualify.py").external_attr >> 16
        assert stat.S_IMODE(script_mode) == 0o755
        assert stat.S_IMODE(archive.getinfo("skills/sales/SKILL.md").external_attr >> 16) == 0o644


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_raw_mcp_credentials_become_slots_and_unmanaged_commands_are_blocked(
    tmp_path, runtime_kind
):
    remote = {
        "transport": "streamable-http",
        "url": "https://crm.example.test/mcp",
        "headers": {"Authorization": "Bearer PRIVATE-TOKEN"},
        "tools": {"include": ["search_contacts"]},
        "toolFilter": {"include": ["search_contacts"]},
    }
    local = {
        "command": "npx",
        "args": ["-y", "unversioned-crm", "PRIVATE-TOKEN"],
        "env": {"HUBSPOT_TOKEN": "PRIVATE-TOKEN"},
    }
    native = {"hubspot": remote, "local": local}
    config = {"mcp_servers": native} if runtime_kind == "hermes" else {"mcp": {"servers": native}}
    result = inspect(state(tmp_path, runtime_kind, **config), runtime_kind)

    serialized = json.dumps(result["manifest"])
    assert "PRIVATE-TOKEN" not in serialized
    assert "unversioned-crm" not in serialized
    connectors = {c["id"]: c for c in result["manifest"]["connectors"]}
    assert connectors["hubspot"]["headers"]["Authorization"] == {
        "slot": "hubspot",
        "field": "headers_Authorization",
    }
    assert connectors["local"]["env"]["HUBSPOT_TOKEN"] == {
        "slot": "local",
        "field": "env_HUBSPOT_TOKEN",
    }
    assert connectors["hubspot"]["tools"] == ["search_contacts"]
    assert any(b["kind"] == "local-payload" for b in result["manifest"]["unresolved"])
    assert "command" not in connectors["local"] and connectors["local"]["args"] == []


def test_embedded_url_credentials_and_oauth_are_not_exported(tmp_path):
    config = {
        "mcp": {
            "servers": {
                "crm": {
                    "url": "https://user:PRIVATE@example.test/mcp?token=PRIVATE#PRIVATE",
                    "oauth": {"clientSecret": "PRIVATE"},
                }
            }
        }
    }
    result = inspect(state(tmp_path, **config))
    assert "PRIVATE" not in json.dumps(result)
    connector = result["manifest"]["connectors"][0]
    assert connector["url"] == "https://example.test/mcp"
    assert {b["kind"] for b in result["manifest"]["unresolved"]} >= {"url-credentials", "oauth"}


def test_symlinks_hardlinks_and_private_skill_files_are_blockers(tmp_path):
    root = state(tmp_path)
    put(root, "skills/sales/SKILL.md", b"Sales process")
    private = put(root, "credentials/auth.json", b"PRIVATE")
    (root / "skills/sales/leak").symlink_to(private)
    (root / "skills/sales/hardlink").hardlink_to(private)
    put(root, "skills/sales/.env", b"PRIVATE")
    result = inspect(root)
    assert set(result["files"]) == {"skills/sales/SKILL.md"}
    assert {b["kind"] for b in result["manifest"]["unresolved"]} == {"excluded-file"}
    assert "PRIVATE" not in json.dumps(result["manifest"])


def test_external_paths_cannot_read_beyond_the_state_volume(tmp_path):
    config = {"skills": {"load": {"extraDirs": ["/etc", "../private", "${SECRET}/skills"]}}}
    result = inspect(state(tmp_path, **config))
    assert not result["files"]
    assert {b["kind"] for b in result["manifest"]["unresolved"]} == {"external-path"}


def test_skill_precedence_and_disabled_status_are_reviewable(tmp_path):
    root = state(tmp_path, skills={"entries": {"disabled": {"enabled": False}}})
    for path in ("workspace/skills/crm", "skills/crm", "skills/disabled"):
        put(
            root,
            path + "/SKILL.md",
            ("---\nname: " + path.rsplit("/", 1)[-1] + "\n---\nSales process").encode(),
        )
    result = inspect(root)
    candidates = {c["source"]: c for c in result["metadata"]["candidates"]}
    assert candidates["workspace/skills/crm"]["selected"] is True
    assert candidates["skills/crm"]["shadowed"] is True
    assert candidates["skills/crm"]["selected"] is False
    assert candidates["skills/disabled"]["enabled"] is False
    validate_manifest(result["manifest"], result["files"])


def test_untrusted_hermes_project_skill_is_captured_unselected(tmp_path):
    root = state(tmp_path, "hermes")
    put(root, "workspace/.agents/skills/crm/SKILL.md", b"CRM")
    result = inspect(root, "hermes")
    assert result["manifest"]["skills"][0]["enabled"] is False
    assert result["metadata"]["candidates"][0]["trusted"] is False


@pytest.mark.parametrize("executable", [False, True])
def test_verified_managed_receipt_retains_connector_payload_and_hashes(tmp_path, executable):
    native_server = {"command": "python3", "args": ["/not-executed"], "env": {"TOKEN": "${ENV}"}}
    root = state(tmp_path, mcp={"servers": {"talos-crm": native_server}})
    content = b"# a reproducible inert connector\n"
    path = "connectors/crm/main.py"
    digest = hashlib.sha256(content).hexdigest()
    artifact_hash = "a" * 64
    original = {
        "instructions": "Sales policy",
        "skills": [],
        "connectors": [
            {
                "id": "crm",
                "name": "CRM",
                "enabled": True,
                "transport": "stdio",
                "tools": ["lookup"],
                "runner": "python3",
                "entrypoint": path,
                "env": {"TOKEN": {"slot": "crm", "field": "token"}},
                "provenance": "Pure stdlib fixture v1",
                "args": [],
                "headers": {},
            }
        ],
        "connection_slots": [{"id": "crm", "label": "CRM", "fields": ["token"]}],
        "assets": {path: digest},
    }
    payload = put(root, f".talos/setups/{artifact_hash}/{path}", content)
    if executable:
        original["executables"] = [path]
        payload.chmod(0o700)
    receipt = {
        "artifact_hash": artifact_hash,
        "manifest": original,
        "servers": {"talos-crm": native_server},
    }
    put(root, ".talos/setup-receipt.json", json.dumps(receipt).encode())
    result = inspect(root)
    assert result["files"] == {path: content}
    assert result["manifest"]["assets"][path] == digest
    assert result["manifest"].get("executables", []) == ([path] if executable else [])
    assert result["manifest"]["unresolved"] == []
    assert result["manifest"]["instructions"] == "Sales policy"
    validate_manifest(result["manifest"], result["files"])

    put(root, f".talos/setups/{artifact_hash}/{path}", b"modified")
    result = inspect(root)
    assert "managed-drift" in {b["kind"] for b in result["manifest"]["unresolved"]}
    assert path not in result["files"]


def client_fixture(running=False):
    labels = {"io.talos.agent": "fixture"}
    incarnation = SimpleNamespace(
        image_digest="sha256:" + "a" * 64,
        runtime_release=RUNTIME_RELEASES["openclaw"],
        container_id="source",
        container_name="source",
        config_volume="fixture-config",
    )
    source = Mock(labels=labels, status="running" if running else "exited")
    source.attrs = {"State": {"Running": running}}
    helper = Mock()
    helper.attach.return_value = iter([b'{"manifest":{},"files":{},"metadata":{}}'])
    helper.wait.return_value = {"StatusCode": 0}
    client = Mock()
    client.volumes.get.return_value.attrs = {"Labels": labels}
    client.images.get.return_value = SimpleNamespace(
        id=incarnation.image_digest,
        attrs={"Architecture": "arm64"},
    )
    client.containers.get.side_effect = lambda name: (
        source if name == "source" else ((_ for _ in ()).throw(NotFound("missing")))
    )
    client.containers.create.return_value = helper
    return client, incarnation, labels, helper


def test_capture_helper_is_pinned_offline_readonly_and_without_persistent_logs():
    client, incarnation, labels, helper = client_fixture()
    assert capture_setup(client, "state-volume", incarnation, "openclaw", labels)["files"] == {}
    args, options = client.containers.create.call_args
    assert args == (incarnation.image_digest,)
    assert options["network_mode"] == "none" and options["read_only"] is True
    assert options["volumes"] == {"state-volume": {"bind": "/state", "mode": "ro"}}
    assert options["log_config"] == {"type": "none"}
    assert "load_config" not in options["command"][0]
    helper.remove.assert_called_once_with(force=True)


def test_running_source_is_rejected_before_helper_creation():
    client, incarnation, labels, _ = client_fixture(running=True)
    with pytest.raises(SetupCaptureError, match="Stop the agent"):
        capture_setup(client, "state-volume", incarnation, "openclaw", labels)
    client.containers.create.assert_not_called()


def test_helper_failure_never_exposes_native_errors():
    client, incarnation, labels, helper = client_fixture()
    helper.wait.return_value = {"StatusCode": 1}
    helper.attach.return_value = iter([b"PRIVATE-NATIVE-CONFIG"])
    with pytest.raises(SetupCaptureError, match="Native setup capture failed") as error:
        capture_setup(client, "state-volume", incarnation, "openclaw", labels)
    assert "PRIVATE" not in str(error.value)
    helper.remove.assert_called_once_with(force=True)


def test_managed_enabled_skill_directory_is_not_reported_as_external(tmp_path):
    artifact_hash = "a" * 64
    skill = b"---\nname: sales\n---\nSales guide.\n"
    root = state(
        tmp_path,
        skills={
            "load": {
                "extraDirs": [
                    f"/home/node/.openclaw/.talos/setups/{artifact_hash}/enabled-skills",
                ]
            }
        },
    )
    assets = {"skills/sales/SKILL.md": hashlib.sha256(skill).hexdigest()}
    receipt = {
        "artifact_hash": artifact_hash,
        "manifest": {
            "assets": assets,
            "skills": [{"id": "sales", "name": "sales", "path": "skills/sales", "enabled": True}],
        },
        "file_hashes": assets
        | {"enabled-skills/sales/SKILL.md": hashlib.sha256(skill).hexdigest()},
    }
    put(root, ".talos/setup-receipt.json", json.dumps(receipt).encode())
    put(root, f".talos/setups/{artifact_hash}/skills/sales/SKILL.md", skill)
    exposed = put(root, f".talos/setups/{artifact_hash}/enabled-skills/sales/SKILL.md", skill)
    result = inspect(root)
    assert not result["manifest"]["unresolved"]
    assert result["files"] == {"skills/sales/SKILL.md": skill}
    exposed.write_bytes(b"changed native content")
    result = inspect(root)
    assert "managed-drift" in {b["kind"] for b in result["manifest"]["unresolved"]}


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
@pytest.mark.parametrize("drift_path", ["skills/sales/run.sh", "enabled-skills/sales/run.sh"])
@pytest.mark.parametrize("executable", [False, True])
def test_managed_capture_checks_original_and_enabled_copy_executable_modes(
    tmp_path, runtime_kind, drift_path, executable
):
    root = state(tmp_path, runtime_kind)
    artifact_hash = "a" * 64
    original_path = "skills/sales/run.sh"
    enabled_path = "enabled-skills/sales/run.sh"
    content = b"#!/bin/sh\nprintf 'portable fixture\\n'\n"
    skill = b"---\nname: sales\n---\nRead the sales guide.\n"
    files = {"skills/sales/SKILL.md": skill, original_path: content}
    hashes = {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
    receipt = {
        "artifact_hash": artifact_hash,
        "manifest": {
            "assets": hashes,
            "executables": [original_path] if executable else [],
            "skills": [{"id": "sales", "name": "sales", "path": "skills/sales"}],
        },
        "file_hashes": hashes | {enabled_path: hashes[original_path]},
        "executables": [original_path, enabled_path] if executable else [],
    }
    put(root, ".talos/setup-receipt.json", json.dumps(receipt).encode())
    for path, data in files.items():
        target = put(root, f".talos/setups/{artifact_hash}/{path}", data)
        target.chmod(0o700 if executable and path == original_path else 0o600)
    target = put(root, f".talos/setups/{artifact_hash}/{enabled_path}", content)
    target.chmod(0o700 if executable else 0o600)
    result = inspect(root, runtime_kind)
    assert result["manifest"]["unresolved"] == []
    assert result["files"] == files
    # Only portable source paths belong in the draft, never derived runtime paths.
    assert result["manifest"].get("executables", []) == ([original_path] if executable else [])

    target = root / f".talos/setups/{artifact_hash}/{drift_path}"
    for permissions in (0o600, 0o610) if executable else (0o700, 0o610):
        target.chmod(permissions)
        result = inspect(root, runtime_kind)
        assert "managed-drift" in {item["kind"] for item in result["manifest"]["unresolved"]}
        assert original_path not in result["files"]
        assert not result["manifest"].get("executables")


def test_setup_free_receipt_does_not_block_first_capture(tmp_path):
    root = state(tmp_path)
    put(root, ".talos/setup-receipt.json", b'{"artifact_hash":null,"manifest":{}}')
    put(root, "skills/sales/SKILL.md", b"Sales")
    assert inspect(root)["manifest"]["unresolved"] == []


def test_installed_plugins_are_visible_without_reading_or_running_their_code(tmp_path):
    root = state(tmp_path, "hermes")
    put(root, "plugins/unsafe/plugin.py", b"raise RuntimeError('PRIVATE')")
    result = inspect(root, "hermes")
    assert result["files"] == {}
    assert "PRIVATE" not in json.dumps(result)
    assert result["manifest"]["unresolved"][0]["kind"] == "unsupported-plugin"
    assert result["metadata"]["candidates"][0]["kind"] == "plugin"
