import asyncio
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event, text

from backend.app.config import get_settings
from tests.admin_client import administrator_client
from tests.integration import test_agents
from tests.unit.test_setup_bundles import bundle_fixture

database_engine = test_agents.database_engine
api_for = test_agents.api_for
pytestmark = pytest.mark.integration


@pytest.fixture
def client(database_engine, tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(get_settings(), "setup_artifacts_dir", tmp_path / "artifacts")
    with database_engine.begin() as connection:
        connection.execute(text("TRUNCATE setups CASCADE"))
    sessions = sessionmaker(database_engine, expire_on_commit=False)
    with administrator_client(api_for(sessions), sessions) as client:
        yield client


def upload(client, manifest, files, **params):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for path, data in files.items():
            archive.writestr(path, data)
    return client.post(
        "/api/v1/setups/import",
        params=params,
        content=content.getvalue(),
        headers={"Content-Type": "application/zip"},
    )


def test_import_publish_export_and_new_installation_reproduction(client):
    manifest, files = bundle_fixture()
    imported = upload(client, manifest, files, name="Sales")
    assert imported.status_code == 201
    setup = imported.json()
    path = "/api/v1/setups/" + setup["id"]
    assert client.get(path + "/validation").json() == {"valid": True, "errors": []}
    asset = client.get(path + "/draft/assets", params={"path": "skills/qualify/SKILL.md"})
    assert asset.content == files["skills/qualify/SKILL.md"]
    first = client.post(path + "/revisions")
    assert first.status_code == 201
    first = first.json()
    assert first["version"] == 1
    manifest["instructions"] = "Changed in editable draft"
    assert client.put(path + "/draft", json={"manifest": manifest}).status_code == 200
    second = client.post(path + "/revisions").json()
    assert second["version"] == 2 and second["artifact_hash"] != first["artifact_hash"]
    revisions = client.get(path).json()["revisions"]
    assert revisions[0]["manifest"]["instructions"] == ""
    assert revisions[1]["manifest"]["instructions"] == "Changed in editable draft"
    exported = client.get(path + f"/revisions/{first['id']}/export")
    assert exported.status_code == 200
    copied = client.post("/api/v1/setups/import", content=exported.content).json()
    assert copied["id"] != setup["id"]
    copy_revision = client.post(f"/api/v1/setups/{copied['id']}/revisions").json()
    assert copy_revision["artifact_hash"] == first["artifact_hash"]
    assert copy_revision["manifest"]["assets"] == first["manifest"]["assets"]
    assert client.delete(path).status_code == 409


def test_draft_metadata_edit_validation_and_safe_failure(client):
    setup = client.post("/api/v1/setups", json={"name": "Draft"}).json()
    path = "/api/v1/setups/" + setup["id"]
    assert not client.get(path + "/validation").json()["valid"]
    assert client.post(path + "/revisions").status_code == 422
    assert client.put(path, json={"name": "Renamed"}).json()["name"] == "Renamed"
    manifest, files = bundle_fixture()
    assert upload(client, manifest, files, setup_id=setup["id"]).json()["id"] == setup["id"]
    manifest["connectors"][0]["headers"]["Authorization"] = "private-token-never-return"
    assert client.put(path + "/draft", json={"manifest": manifest}).status_code == 200
    rejected = client.post(path + "/revisions")
    assert rejected.status_code == 422 and "private-token-never-return" not in rejected.text
    assert client.get(path).json()["revisions"] == []
    assert client.delete(path).status_code == 204


def test_rejects_hash_tampering_and_missing_asset_inspection(client):
    manifest, files = bundle_fixture()
    manifest["assets"]["skills/qualify/SKILL.md"] = "0" * 64
    assert upload(client, manifest, files).status_code == 422
    assert client.get("/api/v1/setups").json() == []
    setup = client.post("/api/v1/setups", json={"name": "Draft"}).json()
    path = f"/api/v1/setups/{setup['id']}/draft/assets"
    assert client.get(path, params={"path": "../private"}).status_code == 422
    assert client.get(path, params={"path": "skills/missing/SKILL.md"}).status_code == 404


def test_draft_export_and_explicit_asset_removal(client):
    manifest, files = bundle_fixture()
    setup = upload(client, manifest, files).json()
    path = "/api/v1/setups/" + setup["id"]
    assert client.get(path + "/draft/export").status_code == 200
    manifest["skills"] = []
    manifest["assets"] = {}
    edited = client.put(path + "/draft", json={"manifest": manifest})
    assert edited.status_code == 200 and edited.json()["draft_manifest"]["assets"] == {}
    assert client.post(path + "/revisions").status_code == 201
    manifest["assets"] = {"unowned/file": "0" * 64}
    assert client.put(path + "/draft", json={"manifest": manifest}).status_code == 422


def test_import_processing_does_not_block_other_api_requests(client, database_engine, monkeypatch):
    from backend.app import setups

    entered, release = Event(), Event()
    read_bundle = setups.read_bundle

    def slow_bundle(*args, **kwargs):
        entered.set()
        assert release.wait(10), "Import was never released"
        return read_bundle(*args, **kwargs)

    def sql_off_event_loop(*_):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        pytest.fail("Import or response serialization ran SQL on the API event loop")

    monkeypatch.setattr(setups, "read_bundle", slow_bundle)
    event.listen(database_engine, "before_cursor_execute", sql_off_event_loop)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            importing = executor.submit(upload, client, *bundle_fixture())
            try:
                assert entered.wait(5), "Import did not start"
                health = executor.submit(client.get, "/health/live")
                assert health.result(timeout=2).status_code == 200
                assert not importing.done()
            finally:
                release.set()
            assert importing.result(timeout=5).status_code == 201
    finally:
        event.remove(database_engine, "before_cursor_execute", sql_off_event_loop)


def test_failed_import_commit_preserves_existing_draft(client, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from backend.app.models import Setup

    setup = client.post("/api/v1/setups", json={"name": "Original draft"}).json()
    commit = Session.commit

    def fail_import(session):
        if any(isinstance(row, Setup) for row in session.dirty):
            raise SQLAlchemyError("private database details")
        return commit(session)

    monkeypatch.setattr(Session, "commit", fail_import)
    result = upload(client, *bundle_fixture(), setup_id=setup["id"])
    assert result.status_code == 503 and "private database details" not in result.text
    restored = client.get(f"/api/v1/setups/{setup['id']}").json()
    assert restored["draft_manifest"] == setup["draft_manifest"]
    assert restored["draft_artifact_hash"] is None


def test_import_size_limit_rejects_before_processing(client, monkeypatch):
    from backend.app import setups

    monkeypatch.setattr(setups, "MAX_ARCHIVE_BYTES", 1)
    assert upload(client, *bundle_fixture()).status_code == 413
    assert client.get("/api/v1/setups").json() == []
