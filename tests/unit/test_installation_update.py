"""Update ordering and crash boundaries; all Docker/backup effects are synthetic."""

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest

from backend.management import backup as snapshots
from backend.management import installation as host
from backend.management import update as updates


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    root.mkdir()
    bundle = tmp_path / "release"
    bundle.mkdir()
    platform = "linux/arm64"
    source = {"version": "0.1.0", "database_revision": "0026"}
    target = {
        "version": "0.2.0",
        "database_revision": "0027",
        "compatible_from": [source.copy()],
        "images": {platform: {"platform": "ghcr.io/test/platform@sha256:" + "a" * 64}},
        "runtime_versions": {platform: {"openclaw": {"2026.9.6": "new-wrapper"}}},
    }
    (bundle / "manifest.json").write_text(json.dumps(target))
    (bundle / "compose.release.yaml").write_text("services: {}\n")
    installation = host.Installation(root)
    installation.state = {
        "schema_version": 1,
        "installation_id": "installation-test",
        "compose_project": "talos-test",
        "platform": platform,
        "release": source["version"],
        "access": {"mode": "local", "port": 8000},
        "docker_socket": "/var/run/docker.sock",
        "owner_uid": os.getuid(),
        "owner_gid": os.getgid(),
    }
    installation.manifest = copy.deepcopy(source)
    installation.save()
    installation.write("manifest.json", source)
    catalog = {"openclaw": {"2026.8.1": "old-image"}, "hermes": {"0.21.5": "old-hermes"}}
    (root / ".env").write_text(
        "POSTGRES_PASSWORD=unchanged-secret\nTALOS_RUNTIME_VERSIONS=" + json.dumps(catalog) + "\n"
    )
    archive, identity = tmp_path / "backup.tar", tmp_path / "recovery-key"
    identity.write_text("fake test identity")
    monkeypatch.setattr(updates, "checked_bundle", lambda _: copy.deepcopy(target))
    monkeypatch.setattr(host, "checked_bundle", lambda _: copy.deepcopy(target))
    events = []
    snapshot_overrides = {}

    def db_command(code, *args, **kwargs):
        events.append(("database", code))
        if "alembic_version" in code:
            return SimpleNamespace(stdout=installation.manifest["database_revision"] + "\n")
        return SimpleNamespace(stdout="")

    installation.db_command = Mock(side_effect=db_command)
    installation.compose = Mock(side_effect=lambda *a, **k: events.append(("compose", a)))
    installation.ready = Mock(side_effect=lambda: events.append(("ready",)))

    def backup(installation, archive, identity, **kwargs):
        events.append(("backup",))
        archive.write_bytes(b"synthetic verified backup")
        return archive

    @contextmanager
    def read_backup(archive, identity):
        events.append(("validate-backup",))
        snapshot = {
            "operation_id": installation.read("operation.json")["operation_id"],
            "installation_id": installation.state["installation_id"],
            "release": source.copy(),
        }
        snapshot.update(snapshot_overrides)
        yield snapshot, tmp_path, {}

    def restore(installation, archive, identity, **kwargs):
        events.append(("restore",))
        installation.manifest = source.copy()
        installation.state["release"] = source["version"]
        installation.write("manifest.json", source)
        installation.save()

    backup_mock = create_autospec(snapshots.backup, side_effect=backup)
    restore_mock = create_autospec(snapshots.restore, side_effect=restore)
    resume_mock = create_autospec(
        snapshots.resume, side_effect=lambda *a, **k: events.append(("resume",))
    )
    monkeypatch.setattr(updates, "backup", backup_mock)
    monkeypatch.setattr(updates, "read_backup", read_backup)
    monkeypatch.setattr(updates, "restore", restore_mock)
    monkeypatch.setattr(updates, "resume", resume_mock)
    control = Mock(return_value={"maintenance": {"active": True}})
    monkeypatch.setattr(snapshots, "_control", control)

    def interrupted(phase):
        return installation.journal(
            "update",
            phase,
            target_manifest=hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest(),
            archive=str(archive),
            identity=str(identity),
            previous_release=source["version"],
        )

    return SimpleNamespace(
        installation=installation,
        bundle=bundle,
        archive=archive,
        identity=identity,
        source=source,
        target=target,
        catalog=catalog,
        events=events,
        backup=backup_mock,
        restore=restore_mock,
        resume=resume_mock,
        control=control,
        snapshot_overrides=snapshot_overrides,
        interrupted=interrupted,
        run=lambda: updates.update(installation, bundle, archive, identity),
    )


@pytest.mark.parametrize("mismatch", ["version", "database", "undeclared"])
def test_incompatible_release_is_rejected_before_mutation(scenario, mismatch):
    if mismatch == "undeclared":
        scenario.target["compatible_from"] = []
    elif mismatch == "version":
        scenario.target["compatible_from"][0]["version"] = "0.0.9"
    else:
        scenario.target["compatible_from"][0]["database_revision"] = "0025"
    with pytest.raises(ValueError, match="compatibility"):
        scenario.run()
    assert not scenario.installation.read("operation.json")
    assert scenario.events == []
    scenario.backup.assert_not_called()
    scenario.installation.compose.assert_not_called()


def test_database_revision_mismatch_is_rejected_before_maintenance(scenario):
    scenario.installation.db_command.return_value = SimpleNamespace(stdout="wrong-revision\n")
    scenario.installation.db_command.side_effect = None
    with pytest.raises(ValueError, match="database revision"):
        scenario.run()
    assert not scenario.installation.read("operation.json")
    scenario.backup.assert_not_called()
    scenario.restore.assert_not_called()
    scenario.installation.compose.assert_not_called()


def test_success_uses_own_backup_and_preserves_installed_runtime_catalog(scenario):
    scenario.run()
    operation = scenario.installation.read("operation.json")
    scenario.backup.assert_called_once_with(
        scenario.installation,
        scenario.archive,
        scenario.identity,
        operation_id=operation["operation_id"],
        keep_maintenance=True,
        kind="update",
    )
    environment = dict(
        line.split("=", 1)
        for line in (scenario.installation.directory / ".env").read_text().splitlines()
    )
    assert json.loads(environment["TALOS_RUNTIME_VERSIONS"]) == scenario.catalog
    assert environment["POSTGRES_PASSWORD"] == "unchanged-secret"
    assert scenario.installation.manifest["version"] == scenario.target["version"]
    assert operation["phase"] == "complete"
    assert scenario.events.index(("validate-backup",)) < next(
        index for index, event in enumerate(scenario.events) if event[0] == "compose"
    )
    scenario.restore.assert_not_called()
    scenario.resume.assert_called_once_with(scenario.installation, operation["operation_id"])


@pytest.mark.parametrize("field", ["operation_id", "installation_id", "release"])
def test_existing_archive_must_match_this_operation_installation_and_release(scenario, field):
    scenario.interrupted("backup")
    scenario.archive.write_bytes(b"already exists")
    scenario.snapshot_overrides[field] = {"version": "0.0.9"} if field == "release" else "foreign"
    with pytest.raises(ValueError, match="own verified snapshot"):
        scenario.run()
    assert scenario.installation.manifest == scenario.source
    assert scenario.installation.read("operation.json")["phase"] == "backup"
    scenario.backup.assert_not_called()
    scenario.restore.assert_not_called()
    scenario.installation.compose.assert_not_called()


def test_failed_health_before_reopening_restores_matching_snapshot(scenario):
    scenario.installation.ready.side_effect = RuntimeError("synthetic readiness failure")
    with pytest.raises(ValueError, match="before reopening traffic"):
        scenario.run()
    operation = scenario.installation.read("operation.json")
    scenario.restore.assert_called_once_with(
        scenario.installation,
        scenario.archive,
        scenario.identity,
        rollback=True,
        operation_id=operation["operation_id"],
    )
    assert scenario.installation.manifest == scenario.source
    assert operation["phase"] == "complete" and operation["result"] == "rolled_back"
    assert scenario.events.index(("restore",)) < scenario.events.index(("resume",))


def test_migration_revision_mismatch_rolls_back_before_reopening(scenario):
    scenario.installation.db_command.side_effect = [
        SimpleNamespace(stdout="0026\n"),
        SimpleNamespace(stdout="0026\n"),
    ]
    with pytest.raises(ValueError, match="before reopening traffic"):
        scenario.run()
    assert scenario.installation.manifest == scenario.source
    scenario.restore.assert_called_once()
    scenario.installation.ready.assert_not_called()


@pytest.mark.parametrize("phase", ["apply", "migrate", "verify", "rollback"])
def test_interrupted_pretraffic_mutation_rolls_back_before_retry(scenario, phase):
    prior = scenario.interrupted(phase)
    with pytest.raises(ValueError, match="Interrupted update rolled back"):
        scenario.run()
    scenario.restore.assert_called_once_with(
        scenario.installation,
        scenario.archive,
        scenario.identity,
        rollback=True,
        operation_id=prior["operation_id"],
    )
    scenario.backup.assert_not_called()
    scenario.resume.assert_called_once_with(scenario.installation, prior["operation_id"])
    assert scenario.installation.read("operation.json")["result"] == "rolled_back"


@pytest.mark.parametrize("changed", ["bundle", "archive", "identity"])
def test_interrupted_update_requires_identical_target_and_paths(scenario, changed):
    scenario.interrupted("verify")
    if changed == "bundle":
        (scenario.bundle / "manifest.json").write_text("changed bytes")
    elif changed == "archive":
        scenario.archive = scenario.archive.with_name("different.tar")
    else:
        scenario.identity = scenario.identity.with_name("different.key")
    with pytest.raises(ValueError, match="identical bundle and backup paths"):
        updates.update(
            scenario.installation,
            scenario.bundle,
            scenario.archive,
            scenario.identity,
        )
    scenario.backup.assert_not_called()
    scenario.restore.assert_not_called()
    scenario.resume.assert_not_called()


@pytest.mark.parametrize("active", [False, True])
def test_reopening_never_rolls_back_after_possible_traffic(scenario, active):
    prior = scenario.interrupted("reopening")
    scenario.control.return_value = {"maintenance": {"active": active}}
    scenario.run()
    scenario.restore.assert_not_called()
    scenario.backup.assert_not_called()
    if active:
        scenario.resume.assert_called_once_with(scenario.installation, prior["operation_id"])
    else:
        scenario.resume.assert_not_called()
    assert scenario.installation.read("operation.json")["phase"] == "complete"


def test_failed_reopening_readiness_keeps_unknown_boundary_without_rollback(scenario):
    scenario.interrupted("reopening")
    scenario.control.return_value = {"maintenance": {"active": False}}
    scenario.installation.ready.side_effect = RuntimeError("synthetic reconnect failure")
    with pytest.raises(RuntimeError, match="reconnect failure"):
        scenario.run()
    assert scenario.installation.read("operation.json")["phase"] == "reopening"
    scenario.restore.assert_not_called()
    scenario.resume.assert_not_called()


@pytest.mark.parametrize("rollback", [False, True])
def test_lost_resume_response_never_causes_another_snapshot_restore(scenario, rollback):
    if rollback:
        scenario.installation.ready.side_effect = RuntimeError("pretraffic health failure")
    scenario.resume.side_effect = RuntimeError("response lost after releasing admission")
    with pytest.raises(RuntimeError, match="response lost"):
        scenario.run()
    # The next process cannot know whether resume admitted traffic. It must only
    # check readiness/status, including when reopening the rolled-back release.
    assert scenario.installation.read("operation.json")["phase"] == "reopening"
    prior_restores = scenario.restore.call_count
    scenario.installation.ready.side_effect = None
    scenario.resume.side_effect = None
    scenario.control.return_value = {"maintenance": {"active": False}}
    scenario.run()
    assert scenario.restore.call_count == prior_restores
    assert scenario.installation.read("operation.json")["phase"] == "complete"


def test_interrupted_backup_reuses_operation_and_retries_without_applying_early(scenario):
    scenario.backup.side_effect = RuntimeError("snapshot interrupted")
    with pytest.raises(RuntimeError, match="snapshot interrupted"):
        scenario.run()
    operation = scenario.installation.read("operation.json")
    assert operation["phase"] == "backup"
    assert scenario.installation.manifest == scenario.source
    scenario.installation.compose.assert_not_called()
    scenario.restore.assert_not_called()
    scenario.backup.side_effect = lambda *a, **kw: scenario.archive.write_bytes(b"finished")
    scenario.run()
    assert scenario.installation.read("operation.json")["operation_id"] == operation["operation_id"]
    assert scenario.installation.read("operation.json")["phase"] == "complete"


def test_interrupted_rollback_reopening_starts_services_before_readiness(scenario):
    scenario.interrupted("reopening")
    scenario.installation.ready.side_effect = RuntimeError("restored API is stopped")
    scenario.control.return_value = {"maintenance": {"active": True}}
    scenario.run()
    scenario.installation.ready.assert_not_called()
    scenario.resume.assert_called_once()
    scenario.restore.assert_not_called()


def test_snapshot_without_matching_maintenance_fence_never_applies(scenario):
    scenario.interrupted("backup")
    scenario.archive.write_bytes(b"existing snapshot")
    scenario.control.side_effect = RuntimeError("maintenance owner mismatch")
    with pytest.raises(RuntimeError, match="owner mismatch"):
        scenario.run()
    scenario.installation.compose.assert_not_called()
    scenario.restore.assert_not_called()
    assert scenario.installation.manifest == scenario.source
