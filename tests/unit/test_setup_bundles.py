import hashlib
import io
import json
import stat
import zipfile

import pytest

from backend.app.config import get_settings
from backend.app.setups import (
    BundleError,
    empty_manifest,
    load_bundle,
    read_bundle,
    store_bundle,
    validate_manifest,
    write_bundle,
)


def bundle_fixture():
    files = {"skills/qualify/SKILL.md": b"---\nname: qualify\n---\nQualify a lead."}
    manifest = {
        **empty_manifest(),
        "targets": [
            {"runtime_kind": "hermes", "runtime_release": "hermes-0.21.5", "architecture": "arm64"}
        ],
        "skills": [{"id": "qualify", "name": "qualify", "path": "skills/qualify"}],
        "connectors": [
            {
                "id": "crm",
                "name": "CRM",
                "transport": "streamable-http",
                "tools": ["search"],
                "url": "https://crm.example/mcp",
                "headers": {"Authorization": {"slot": "crm", "field": "TOKEN"}},
            }
        ],
        "connection_slots": [{"id": "crm", "label": "CRM account", "fields": ["TOKEN"]}],
        "assets": {path: hashlib.sha256(value).hexdigest() for path, value in files.items()},
    }
    return manifest, files


def archive_with(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def test_canonical_bundle_roundtrip_and_immutable_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "setup_artifacts_dir", tmp_path)
    manifest, files = bundle_fixture()
    normalized = validate_manifest(manifest, files)
    content = write_bundle(normalized, files)
    assert write_bundle(normalized, dict(reversed(list(files.items())))) == content
    imported, restored = read_bundle(content, publication=True)
    assert restored == files and imported == normalized
    digest = store_bundle(content)
    assert digest == store_bundle(content)
    assert load_bundle(digest) == (normalized, files)
    (tmp_path / f"{digest}.zip").write_bytes(b"corrupted")
    with pytest.raises(BundleError, match="integrity"):
        load_bundle(digest)


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "a/../b", "a//b", "a\\b", "a:stream", "a\x01b"]
)
def test_rejects_unsafe_archive_paths(path):
    with pytest.raises(BundleError, match="unsafe"):
        read_bundle(archive_with([(path, b"payload")]))


def test_rejects_links_duplicates_and_asset_hash_tampering():
    link = zipfile.ZipInfo("skill-link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(BundleError, match="links"):
        read_bundle(archive_with([(link, b"../../secret")]))
    with pytest.warns(UserWarning, match="Duplicate"):
        duplicate = archive_with([("same", b"one"), ("same", b"two")])
    with pytest.raises(BundleError, match="duplicate paths"):
        read_bundle(duplicate)
    manifest, files = bundle_fixture()
    manifest["assets"]["skills/qualify/SKILL.md"] = "0" * 64
    with pytest.raises(BundleError, match="hashes"):
        read_bundle(archive_with([("manifest.json", json.dumps(manifest)), *files.items()]))
    with pytest.raises(BundleError, match="duplicate JSON"):
        read_bundle(archive_with([("manifest.json", '{"schema_version":1,"schema_version":1}')]))


def test_limits_are_checked_before_decompression(monkeypatch):
    monkeypatch.setattr("backend.app.setups.MAX_FILE_BYTES", 20)
    with pytest.raises(BundleError, match="size limit"):
        read_bundle(archive_with([("large", b"x" * 21)]))
    monkeypatch.setattr("backend.app.setups.MAX_FILES", 1)
    with pytest.raises(BundleError, match="file count"):
        read_bundle(archive_with([("a", b"a"), ("b", b"b"), ("c", b"c")]))


def test_incomplete_draft_cannot_publish_or_hide_unowned_files():
    draft = empty_manifest()
    assert read_bundle(write_bundle(draft, {}))[0] == draft
    with pytest.raises(BundleError, match="targets"):
        validate_manifest(draft, {})
    manifest, files = bundle_fixture()
    manifest["unresolved"] = [{"message": "Missing dependencies", "item_id": "crm"}]
    with pytest.raises(BundleError, match="blockers"):
        validate_manifest(manifest, files)
    manifest["unresolved"] = []
    files["credentials.json"] = b"private"
    manifest["assets"]["credentials.json"] = hashlib.sha256(b"private").hexdigest()
    with pytest.raises(BundleError, match="Every asset"):
        validate_manifest(manifest, files)


def test_compatibility_and_credentials_are_explicit():
    manifest, files = bundle_fixture()
    manifest["targets"][0]["runtime_release"] = "hermes-latest"
    with pytest.raises(BundleError, match="release"):
        validate_manifest(manifest, files)
    manifest["targets"][0]["runtime_release"] = "hermes-0.21.5"
    connector = manifest["connectors"][0]
    connector["headers"]["Authorization"] = "a-private-token"
    with pytest.raises(BundleError, match="connection reference") as caught:
        validate_manifest(manifest, files)
    assert "a-private-token" not in str(caught.value)
    connector["headers"]["Authorization"] = {"slot": "missing", "field": "TOKEN"}
    with pytest.raises(BundleError, match="undeclared"):
        validate_manifest(manifest, files)
    connector["headers"] = {}
    connector["url"] = "https://username:password@crm.example/mcp"
    with pytest.raises(BundleError, match="embedded credentials"):
        validate_manifest(manifest, files)


def test_local_payload_requires_provenance_entrypoint_and_pinned_interpreter(tmp_path):
    manifest, files = bundle_fixture()
    sentinel = tmp_path / "executed"
    files["connectors/local/server.py"] = f"open({str(sentinel)!r},'w').write('executed')".encode()
    manifest["assets"] = {p: hashlib.sha256(v).hexdigest() for p, v in files.items()}
    manifest["connectors"] = [
        {
            "id": "local",
            "name": "Local",
            "transport": "stdio",
            "runner": "python3",
            "entrypoint": "connectors/local/server.py",
            "provenance": "vendored fixture v1",
            "tools": ["search"],
        }
    ]
    with pytest.raises(BundleError, match="pinned Python"):
        validate_manifest(manifest, files)
    manifest["targets"][0]["python_version"] = "3.11"
    read_bundle(write_bundle(validate_manifest(manifest, files), files), publication=True)
    assert not sentinel.exists()
    manifest["connectors"][0]["entrypoint"] = "connectors/local/missing.py"
    with pytest.raises(BundleError, match="entrypoint"):
        validate_manifest(manifest, files)


def test_skill_shadowing_is_rejected():
    manifest, files = bundle_fixture()
    manifest["skills"].append({"id": "other", "name": "Other", "path": "skills/qualify/nested"})
    with pytest.raises(BundleError, match="overlap"):
        validate_manifest(manifest, files)


def test_enabled_skills_match_native_frontmatter_names():
    manifest, files = bundle_fixture()
    manifest["skills"][0]["name"] = "Wrong display label"
    with pytest.raises(BundleError, match="frontmatter name"):
        validate_manifest(manifest, files)
    manifest["skills"][0]["enabled"] = False
    validate_manifest(manifest, files)
