"""Backup safety checks; Docker/age portability is covered by release acceptance."""

import io
import json
import tarfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.management import backup as snapshots


def tar_bytes(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, kind, target in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = target
            if kind == tarfile.REGTYPE:
                info.size = len(target)
            archive.addfile(info, io.BytesIO(target) if kind == tarfile.REGTYPE else None)
    data.seek(0)
    return data


@pytest.mark.parametrize(
    "entries",
    [
        [("../escape", tarfile.REGTYPE, b"data")],
        [("/escape", tarfile.REGTYPE, b"data")],
        [("link", tarfile.SYMTYPE, "../escape")],
        [("link", tarfile.SYMTYPE, "/escape")],
        [("link", tarfile.SYMTYPE, "nested"), ("link/file", tarfile.REGTYPE, b"data")],
        [("link/file", tarfile.REGTYPE, b"data"), ("link", tarfile.SYMTYPE, "nested")],
        [("same", tarfile.REGTYPE, b"a"), ("./same", tarfile.REGTYPE, b"b")],
        [("device", tarfile.CHRTYPE, "")],
    ],
)
def test_volume_validation_rejects_archive_escape(entries):
    with pytest.raises(snapshots.BackupError):
        snapshots._archive_members(tar_bytes(entries))


def test_volume_validation_preserves_safe_relative_links():
    entries = [
        (".", tarfile.DIRTYPE, ""),
        ("workspace", tarfile.DIRTYPE, ""),
        ("workspace/file", tarfile.REGTYPE, b"secret"),
        ("shortcut", tarfile.SYMTYPE, "workspace/file"),
        ("workspace/parent", tarfile.SYMTYPE, "../workspace/file"),
    ]
    assert "workspace/file" in snapshots._archive_members(tar_bytes(entries))


def test_identity_must_be_private_and_outside_installation(tmp_path):
    identity = tmp_path / "key"
    identity.write_text("fake")
    identity.chmod(0o644)
    with pytest.raises(snapshots.BackupError, match="0600"):
        snapshots._identity(identity)
    identity.chmod(0o600)
    assert snapshots._identity(identity) == identity
    with pytest.raises(snapshots.BackupError, match="outside"):
        snapshots._outside(identity, tmp_path)
    link = tmp_path / "link"
    link.symlink_to(identity)
    with pytest.raises(snapshots.BackupError, match="symlink"):
        snapshots._identity(link)


def test_resume_restarts_services_under_fence_before_releasing(tmp_path, monkeypatch):
    (tmp_path / ".backup-operation.json").write_text(
        json.dumps(
            {"operation_id": "saved-operation", "kind": "backup", "services": ["api", "worker"]}
        )
    )
    events = []
    installation = SimpleNamespace(directory=tmp_path, compose=lambda *args: events.append(args))
    monkeypatch.setattr(
        snapshots,
        "_control",
        lambda _, action, *args: events.append(action) or {"maintenance": {"active": True}},
    )
    snapshots.resume(installation, "saved-operation", verify=False)
    assert events == [
        "status",
        "assert",
        ("up", "-d", "--no-deps", "--pull", "never", "--no-build", "api", "worker"),
        "leave",
    ]
    assert not (tmp_path / ".backup-operation.json").exists()


def test_failed_restart_keeps_maintenance_journal(tmp_path, monkeypatch):
    (tmp_path / ".backup-operation.json").write_text(
        json.dumps({"operation_id": "saved-operation", "kind": "backup", "services": ["api"]})
    )

    def fail(*args):
        raise RuntimeError("Docker unavailable")

    actions = []
    installation = SimpleNamespace(directory=tmp_path, compose=fail)
    monkeypatch.setattr(
        snapshots,
        "_control",
        lambda _, action, *args: actions.append(action) or {"maintenance": {"active": True}},
    )
    with pytest.raises(RuntimeError):
        snapshots.resume(installation, "saved-operation", verify=False)
    assert actions == ["status", "assert"]
    assert (tmp_path / ".backup-operation.json").exists()


def test_foreign_resume_does_not_call_docker_or_database(tmp_path, monkeypatch):
    (tmp_path / ".backup-operation.json").write_text(json.dumps({"operation_id": "owner"}))
    with pytest.raises(snapshots.BackupError, match="owns"):
        snapshots.resume(SimpleNamespace(directory=tmp_path), "intruder")


def test_envelope_rejects_links_before_decryption(tmp_path, monkeypatch):
    identity = tmp_path / "key"
    identity.write_text("fake")
    identity.chmod(0o600)
    archive = tmp_path / "malicious.tar"
    archive.write_bytes(tar_bytes([("manifest.age", tarfile.SYMTYPE, "/etc/passwd")]).getvalue())
    with pytest.raises(snapshots.BackupError, match="envelope"):
        with snapshots.read_backup(archive, identity):
            pytest.fail("Unsafe envelope was accepted")


def test_bad_component_checksum_refuses_restore_before_any_mutation(tmp_path, monkeypatch):
    identity = tmp_path / "key"
    identity.write_text("fake")
    identity.chmod(0o600)
    manifest = {
        "schema_version": 1,
        "components": {"database.age": {"sha256": "f" * 64, "size": 4}},
    }
    archive = tmp_path / "bad-checksum.tar"
    archive.write_bytes(
        tar_bytes(
            [
                ("manifest.age", tarfile.REGTYPE, json.dumps(manifest).encode()),
                ("database.age", tarfile.REGTYPE, b"data"),
            ]
        ).getvalue()
    )

    @contextmanager
    def fake_decrypt(path, identity):
        yield io.BytesIO(path.read_bytes())

    monkeypatch.setattr(snapshots, "_decrypt", fake_decrypt)
    with pytest.raises(snapshots.BackupError, match="checksum"):
        with snapshots.read_backup(archive, identity):
            pytest.fail("Corrupt backup was accepted")


def test_restore_sanitizes_credentials_pending_work_and_channel_ingress():
    from backend.app.connections import Connection
    from backend.app.db import Base
    from backend.app.models import (
        Administrator,
        AdministratorSession,
        Agent,
        ChannelCursor,
        ChannelInbox,
        ChannelOutbox,
        Operation,
        Run,
        UserChannel,
        WorkloadIncarnation,
    )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        administrator = Administrator(id=1, password_hash="fake")
        agent = Agent(display_name="User", user_label="Test", desired_state="running")
        connection = Connection(name="Telegram", purpose="channel")
        session.add_all([administrator, agent, connection])
        session.flush()
        incarnation = WorkloadIncarnation(
            agent_id=agent.id, generation=1, gateway_token_hash="x" * 64, container_id="old"
        )
        channel = UserChannel(
            provider="telegram", name="Chat", enabled=True, connection_id=connection.id
        )
        session.add_all([incarnation, channel])
        session.flush()
        agent.current_incarnation_id = incarnation.id
        run = Run(
            agent_id=agent.id,
            incarnation_id=incarnation.id,
            message="action",
            idempotency_key="one",
            request_hash="f" * 64,
            status="unknown",
        )
        operation = Operation(
            agent_id=agent.id,
            action="start",
            target_revision=1,
            idempotency_scope="test",
            idempotency_key="one",
            request_hash="f" * 64,
        )
        inbox = ChannelInbox(
            channel_id=channel.id,
            channel_revision=1,
            event_id="event",
            external_user_id="user",
            destination="chat",
            code="accepted",
        )
        token = AdministratorSession(
            token_hash="f" * 64, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        session.add_all([run, operation, inbox, token])
        session.flush()
        outbox = ChannelOutbox(inbox_id=inbox.id, state="uncertain")
        session.add(outbox)
        session.commit()
        snapshots.sanitize_restored_database(session)
        session.commit()
        session.expire_all()
        assert agent.desired_state == agent.observed_state == "stopped"
        assert agent.current_incarnation_id is None
        assert incarnation.gateway_token_hash is None and incarnation.revoked_at is not None
        assert session.scalar(select(AdministratorSession)) is None
        assert run.status == "interrupted" and operation.status == "failed"
        assert outbox.state == "blocked" and outbox.code == "restored_quarantine"
        assert channel.enabled is False and channel.verified_version_id is None
        cursor = session.get(ChannelCursor, channel.id)
        assert cursor.reconnect_required and cursor.offset == 0 and cursor.accept_after is None
        assert cursor.code == "restore_reconnect_required"
        # Sanitization preserves the user content and audit identity.
        assert run.message == "action" and run.agent_id == agent.id
        assert session.get(Administrator, 1).password_hash == "fake"


def test_interrupted_rollback_resumes_without_requiring_removed_database(tmp_path, monkeypatch):
    from backend.management.installation import Installation

    installation = Installation(tmp_path)
    installation.state = {"installation_id": "test-install", "compose_project": "test-project"}
    installation.write("operation.json", {"kind": "update", "phase": "rollback"})
    archive = tmp_path / "snapshot"
    archive.write_bytes(b"fixed ciphertext")
    manifest = {
        "installation_id": "test-install",
        "operation_id": "update-owner",
        "platform": "linux/arm64",
        "release": {"images": {"linux/arm64": {"platform": "tested-source-image"}}},
    }
    calls = []
    monkeypatch.setattr(snapshots, "_control", lambda *args, **kwargs: calls.append("assert"))
    monkeypatch.setattr(snapshots, "_ready_existing_database", lambda *args: None)
    monkeypatch.setattr(snapshots, "_containers", lambda *args, **kwargs: [])
    monkeypatch.setattr(snapshots, "_stopped", lambda *args: None)
    installation.compose = lambda *args: calls.append(args)
    snapshots._prepare_rollback(installation, None, manifest, archive, "update-owner")
    assert calls == ["assert", ("stop", "--timeout", "60")]
    assert (tmp_path / ".restore-operation.json").exists()
    calls.clear()
    # Simulate a killed restore after removing its PostgreSQL container/volume.
    snapshots._prepare_rollback(installation, None, manifest, archive, "update-owner")
    assert calls == []
    archive.write_bytes(b"different snapshot")
    with pytest.raises(snapshots.BackupError, match="Another restore"):
        snapshots._prepare_rollback(installation, None, manifest, archive, "update-owner")


def test_rollback_checkpoint_never_authorizes_rollback_after_reopening(tmp_path):
    from backend.management.installation import Installation

    installation = Installation(tmp_path)
    installation.state = {"installation_id": "test-install"}
    installation.write("operation.json", {"kind": "update", "phase": "reopening"})
    manifest = {"installation_id": "test-install", "operation_id": "update-owner"}
    with pytest.raises(snapshots.BackupError, match="Traffic may have resumed"):
        snapshots._prepare_rollback(
            installation, None, manifest, tmp_path / "unused", "update-owner"
        )


def test_resume_keeps_fence_when_readiness_fails(tmp_path, monkeypatch):
    (tmp_path / ".backup-operation.json").write_text(
        json.dumps({"operation_id": "owner", "kind": "backup", "services": ["api"]})
    )
    calls = []

    def fail_readiness():
        raise RuntimeError("API is not ready")

    installation = SimpleNamespace(
        directory=tmp_path, ready=fail_readiness, compose=lambda *args: calls.append("start")
    )
    monkeypatch.setattr(
        snapshots,
        "_control",
        lambda _, action, *args: calls.append(action) or {"maintenance": {"active": True}},
    )
    with pytest.raises(RuntimeError, match="ready"):
        snapshots.resume(installation, "owner")
    assert calls == ["status", "assert", "start"]
    assert (tmp_path / ".backup-operation.json").exists()


def test_published_backup_resumes_without_rewriting_the_archive(tmp_path, monkeypatch):
    from backend.management.installation import Installation

    installation = Installation(tmp_path)
    installation.state = {"installation_id": "source"}
    installation.manifest = {"version": "1.0.0"}
    installation.write(".backup-operation.json", {"operation_id": "owner", "kind": "backup"})
    archive = tmp_path / "finished.archive"
    archive.write_bytes(b"immutable ciphertext")

    @contextmanager
    def saved(*args):
        yield (
            {
                "operation_id": "owner",
                "installation_id": "source",
                "release": installation.manifest,
                "services": ["api"],
            },
            None,
            None,
        )

    monkeypatch.setattr(snapshots, "read_backup", saved)
    monkeypatch.setattr(
        snapshots, "_control", lambda *args: {"maintenance": {"operation_id": "owner"}}
    )
    resumed = []
    monkeypatch.setattr(snapshots, "resume", lambda *args: resumed.append(args[1]))
    assert (
        snapshots._resume_published_backup(
            installation, archive, tmp_path / "identity", "owner", False, "backup"
        )
        == archive
    )
    assert resumed == ["owner"] and archive.read_bytes() == b"immutable ciphertext"
    with pytest.raises(snapshots.BackupError, match="another operation"):
        snapshots._resume_published_backup(
            installation, archive, tmp_path / "identity", "other", False, "backup"
        )


@pytest.mark.parametrize("existing", [True, False])
def test_backup_owns_new_recovery_identity_but_preserves_existing_owner(
    tmp_path, monkeypatch, existing
):
    directory = tmp_path / "installation"
    directory.mkdir()
    identity = tmp_path / "identity"
    if existing:
        identity.write_text("existing private identity")
    installation = SimpleNamespace(
        directory=directory, state={"owner_uid": 1234, "owner_gid": 5678}
    )
    client = SimpleNamespace(close=lambda: None)

    def key(path, **kwargs):
        if not path.exists():
            path.write_text("new private identity")
        return path

    def unavailable(*args):
        raise RuntimeError("No agents stopped yet")

    ownership = []
    monkeypatch.setattr(snapshots, "_identity", key)
    monkeypatch.setattr(snapshots.os, "chown", lambda *args: ownership.append(args))
    monkeypatch.setattr(snapshots.docker, "from_env", lambda: client)
    monkeypatch.setattr(snapshots, "_stopped", unavailable)
    with pytest.raises(RuntimeError, match="stopped"):
        snapshots.backup(installation, tmp_path / "archive", identity)
    assert ownership == ([] if existing else [(identity, 1234, 5678)])


def test_restore_pull_plan_includes_retained_versions_and_deduplicates(tmp_path, monkeypatch):
    current = "ghcr.io/example/talos@sha256:" + "a" * 64
    old = "ghcr.io/example/openclaw@sha256:" + "b" * 64
    unused = "ghcr.io/example/openclaw@sha256:" + "c" * 64

    @contextmanager
    def validated(*args):
        yield (
            {
                "platform": "linux/arm64",
                "release": {
                    "images": {"linux/arm64": {"platform": current}},
                    "runtime_versions": {"linux/arm64": {"openclaw": {"1.0.0": current}}},
                },
                "images": [
                    {"reference": old},
                    {"reference": current},
                    {"component": "image-0.age"},
                ],
            },
            None,
            {
                ".env": (
                    "TALOS_RUNTIME_VERSIONS="
                    + json.dumps(
                        {
                            "openclaw": {"1.0.0": old, "2.0.0": unused},
                            "hermes": {"1.0.0": current},
                        }
                    )
                    + "\n"
                ).encode()
            },
        )

    monkeypatch.setattr(snapshots, "read_backup", validated)
    assert snapshots.list_restore_images(tmp_path / "archive", tmp_path / "identity") == sorted(
        [current, old, unused]
    )


def test_restore_pull_plan_rejects_unvalidated_archive_before_returning_images(
    tmp_path, monkeypatch
):
    @contextmanager
    def invalid(*args):
        raise snapshots.BackupError("Unsupported backup format")
        yield

    monkeypatch.setattr(snapshots, "read_backup", invalid)
    with pytest.raises(snapshots.BackupError, match="Unsupported backup format"):
        snapshots.list_restore_images(tmp_path / "archive", tmp_path / "identity")


def test_new_archive_is_private_and_host_owned_before_atomic_publication(tmp_path, monkeypatch):
    source, destination = tmp_path / "ciphertext.stage", tmp_path / "backup.talos"
    source.write_bytes(b"ciphertext")
    ownership = []

    def chown(path, uid, gid):
        assert not destination.exists()
        ownership.append((path, uid, gid))

    monkeypatch.setattr(snapshots.os, "chown", chown)
    snapshots._publish_snapshot(source, destination, 1234, 5678)
    assert ownership == [(source, 1234, 5678)]
    assert destination.read_bytes() == b"ciphertext"
    assert destination.stat().st_mode & 0o777 == 0o600
    # A collision does not change the previously published artifact or its owner.
    monkeypatch.setattr(snapshots.os, "chown", lambda *args: ownership.append(args))
    source.unlink()
    source.write_bytes(b"different ciphertext")
    with pytest.raises(FileExistsError):
        snapshots._publish_snapshot(source, destination, 9999, 9999)
    assert destination.read_bytes() == b"ciphertext"
    assert all(path == source for path, _, _ in ownership)


def test_new_recovery_identity_is_durable_before_use(tmp_path, monkeypatch):
    import stat

    events = []

    def generate(args, *, stdout):
        stdout.write(b"AGE-SECRET-KEY-fake-recovery-key")
        events.append("generated")

    def synchronize(fd):
        events.append("key" if stat.S_ISREG(snapshots.os.fstat(fd).st_mode) else "directory")

    monkeypatch.setattr(snapshots, "_run", generate)
    monkeypatch.setattr(snapshots.os, "fsync", synchronize)
    identity = snapshots._identity(tmp_path / "identity", create=True)
    assert identity.read_bytes() == b"AGE-SECRET-KEY-fake-recovery-key"
    assert events == ["generated", "key", "directory"]


def test_config_snapshot_preserves_complete_checksummed_release_bundle(tmp_path):
    from backend.management.release import PLATFORMS, verify_bundle
    from deploy import build_release
    from tests.unit.test_release import license_evidence, release_manifest

    manifest = release_manifest()
    installation = tmp_path / "source"
    installation.mkdir()
    for platform in PLATFORMS:
        arch = platform.split("/")[1]
        (tmp_path / f"images-{arch}.json").write_text(
            json.dumps(
                {
                    "platform": platform,
                    "source_revision": manifest["source_revision"],
                    "images": manifest["images"][platform],
                    "license_inventory": license_evidence(tmp_path, manifest, platform),
                }
            )
        )
    build_release.bundle(
        manifest["version"],
        manifest["source_revision"],
        manifest["database_revision"],
        tmp_path,
        installation / "bundle",
        [],
    )
    for name in ("installation.json", "manifest.json", "compose.yaml", ".env"):
        (installation / name).write_text("{}")
    snapshot = snapshots._config_bytes(SimpleNamespace(directory=installation))
    restored = tmp_path / "restored"
    with tarfile.open(fileobj=snapshot) as archive:
        for member in archive:
            target = restored / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    assert (restored / "bundle/Caddyfile.template").is_file()
    assert verify_bundle(restored / "bundle") == manifest
    assert (restored / "bundle/checksums.txt").read_bytes() == (
        installation / "bundle/checksums.txt"
    ).read_bytes()


def test_restore_rejects_unlabeled_named_network_before_mutating_destination(tmp_path, monkeypatch):
    from backend.management import installation as host

    directory = tmp_path / "destination"
    directory.mkdir()
    installation = host.Installation(directory)
    identity = tmp_path / "identity"
    identity.write_text("private fake identity")
    identity.chmod(0o600)
    state = {"installation_id": "source-id", "compose_project": "source-project"}
    (tmp_path / "archive").write_bytes(b"validated snapshot")

    @contextmanager
    def validated(*args):
        yield (
            {"platform": "linux/arm64", "operation_id": "owner", "installation_id": "source-id"},
            None,
            {"installation.json": json.dumps(state).encode()},
        )

    client = SimpleNamespace(info=lambda: {"Architecture": "aarch64"}, close=lambda: None)
    inspections = []

    def inspect(args, **kwargs):
        inspections.append(args)
        if args == ["docker", "network", "inspect", "source-project_control"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Labels": {}}]))
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(snapshots, "read_backup", validated)
    monkeypatch.setattr(snapshots.docker, "from_env", lambda: client)
    monkeypatch.setattr(host, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    monkeypatch.setattr(host.subprocess, "run", inspect)
    with pytest.raises(ValueError, match="Foreign network source-project_control"):
        snapshots.restore(installation, tmp_path / "archive", identity, source_fenced=True)
    assert inspections and list(directory.iterdir()) == []
    assert installation.state == {}


def test_published_backup_cleans_matching_journal_after_fence_release_crash(tmp_path, monkeypatch):
    from backend.management.installation import Installation

    installation = Installation(tmp_path)
    installation.state = {"installation_id": "source"}
    installation.manifest = {"version": "1.0.0"}
    installation.write(
        ".backup-operation.json", {"operation_id": "completed-owner", "kind": "backup"}
    )

    @contextmanager
    def saved(*args):
        yield (
            {
                "operation_id": "completed-owner",
                "installation_id": "source",
                "release": installation.manifest,
            },
            None,
            None,
        )

    monkeypatch.setattr(snapshots, "read_backup", saved)
    monkeypatch.setattr(
        snapshots, "_control", lambda *args: {"maintenance": {"operation_id": None}}
    )
    destination = tmp_path / "published"
    assert (
        snapshots._resume_published_backup(
            installation, destination, tmp_path / "identity", "completed-owner", False, "backup"
        )
        == destination
    )
    assert not (tmp_path / ".backup-operation.json").exists()
    assert (
        snapshots._journal(installation, create=True, operation_id="next-owner")["operation_id"]
        == "next-owner"
    )


def test_finish_resume_preserves_a_different_operations_journal(tmp_path):
    journal = tmp_path / ".backup-operation.json"
    content = json.dumps({"operation_id": "other-owner", "kind": "update"})
    journal.write_text(content)
    with pytest.raises(snapshots.BackupError, match="belongs to another"):
        snapshots.finish_resume(SimpleNamespace(directory=tmp_path), "completed-owner")
    assert journal.read_text() == content


def test_standalone_sanitize_registers_connection_tables_in_fresh_process(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from backend.app.connections import Connection
    from backend.app.db import Base
    from backend.app.models import ChannelCursor, InstallationState, UserChannel

    url = "sqlite:///" + str(tmp_path / "restored.sqlite")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        connection = Connection(name="Restored Telegram", purpose="channel")
        session.add(connection)
        session.flush()
        channel = UserChannel(
            provider="telegram", name="User messages", connection_id=connection.id
        )
        session.add_all(
            [
                channel,
                InstallationState(
                    id=1,
                    maintenance_operation_id="restore-owner",
                    maintenance_kind="backup",
                    maintenance_started_at=datetime.now(UTC),
                ),
            ]
        )
        session.commit()
        channel_id = channel.id
    # The schema exists, but this new interpreter has never imported the API or
    # connection models. A newly inserted cursor forces SQLAlchemy to resolve FKs.
    environment = dict(os.environ, TALOS_DATABASE_URL=url)
    environment.pop("TALOS_DATABASE_PASSWORD", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.management.backup",
            "_control",
            "sanitize",
            "restore-owner",
            "backup",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["maintenance"]["operation_id"] == "restore-owner"
    with Session(engine) as session:
        cursor = session.get(ChannelCursor, channel_id)
        assert cursor is not None and cursor.reconnect_required
        assert cursor.state == "restored" and cursor.offset == 0
        assert not session.get(UserChannel, channel_id).enabled


def test_rollback_restarts_only_owned_database_after_stop_before_checkpoint_crash(
    tmp_path, monkeypatch
):
    from backend.management.installation import Installation

    installation = Installation(tmp_path)
    installation.state = {"installation_id": "source", "compose_project": "source-project"}
    installation.write("operation.json", {"kind": "update", "phase": "rollback"})
    archive = tmp_path / "snapshot"
    archive.write_bytes(b"verified ciphertext")
    manifest = {
        "installation_id": "source",
        "operation_id": "owner",
        "platform": "linux/arm64",
        "release": {"images": {"linux/arm64": {"platform": "source-platform-image"}}},
    }
    events = []

    class Database:
        labels = {
            "com.docker.compose.project": "source-project",
            "com.docker.compose.service": "db",
            "io.talos.installation": "source",
        }
        status = "running"

        def reload(self):
            self.attrs = {"State": {"Status": self.status, "Health": {"Status": "healthy"}}}

        def start(self):
            events.append("start-db")
            self.status = "running"

    database = Database()
    client = SimpleNamespace(
        containers=SimpleNamespace(
            list=lambda all=False, **kwargs: (
                [database] if all or database.status == "running" else []
            )
        )
    )

    def control(_, action, operation_id, kind, *, image):
        assert database.status == "running"
        assert (action, operation_id, kind, image) == (
            "assert",
            "owner",
            "update",
            "source-platform-image",
        )
        events.append("verify-fence")

    def stop(*args):
        assert args == ("stop", "--timeout", "60")
        database.status = "exited"
        events.append("stop")

    installation.compose = stop
    monkeypatch.setattr(snapshots, "_control", control)
    monkeypatch.setattr(snapshots, "_stopped", lambda *args: None)
    durable_write = installation.write

    def killed_before_checkpoint(name, value):
        assert name == ".restore-operation.json"
        raise RuntimeError("process died before checkpoint write")

    installation.write = killed_before_checkpoint
    with pytest.raises(RuntimeError, match="process died"):
        snapshots._prepare_rollback(installation, client, manifest, archive, "owner")
    assert events == ["verify-fence", "stop"]
    assert database.status == "exited" and not (tmp_path / ".restore-operation.json").exists()
    installation.write = durable_write
    snapshots._prepare_rollback(installation, client, manifest, archive, "owner")
    assert events == ["verify-fence", "stop", "start-db", "verify-fence", "stop"]
    assert installation.read(".restore-operation.json")["operation_id"] == "owner"


def test_rollback_database_restart_rejects_foreign_ownership(tmp_path):
    installation = SimpleNamespace(
        state={"installation_id": "source", "compose_project": "project"}
    )
    foreign = SimpleNamespace(
        labels={"com.docker.compose.service": "db", "io.talos.installation": "someone-else"}
    )
    client = SimpleNamespace(containers=SimpleNamespace(list=lambda **kwargs: [foreign]))
    with pytest.raises(snapshots.BackupError, match="Foreign container"):
        snapshots._ready_existing_database(installation, client)


@pytest.fixture
def cold_restore(tmp_path, monkeypatch):
    """Exercise the real restore orchestration with persistent fake Docker resources."""
    import os
    from unittest.mock import Mock

    from docker.errors import NotFound

    from backend.management.installation import Installation

    destination = tmp_path / "destination"
    destination.mkdir()
    archive, identity = tmp_path / "backup.tar", tmp_path / "identity"
    archive.write_bytes(b"authenticated snapshot")
    identity.write_bytes(b"fake private recovery key")
    identity.chmod(0o600)
    state = {
        "schema_version": 1,
        "installation_id": "source",
        "compose_project": "source-project",
        "platform": "linux/arm64",
        "owner_uid": os.getuid(),
        "owner_gid": os.getgid(),
        "docker_socket": "/old/socket",
        "host_os": "macos",
        "phase": "ready",
    }
    labels = {"io.talos.installation": "source", "com.docker.compose.project": "source-project"}
    manifest = {
        "platform": "linux/arm64",
        "installation_id": "source",
        "compose_project": "source-project",
        "operation_id": "snapshot-owner",
        "directory": "/old",
        "images": [],
        "release": {"images": {"linux/arm64": {"management": "helper"}}},
        "services": ["api", "worker", "gateway", "connector"],
        "config_modes": {},
        "volumes": [
            {"name": "database", "labels": labels, "database": True},
            {"name": "state", "labels": labels, "database": False, "component": "volume-1.age"},
        ],
    }
    configs = {
        "installation.json": json.dumps(state).encode(),
        "manifest.json": json.dumps(manifest["release"]).encode(),
        "compose.yaml": b"services: {}\n",
        ".env": b"TALOS_DOCKER_SOCKET=/old/socket\n",
        "bundle/talos": b"#!/bin/bash\n",
        "bundle/manifest.json": json.dumps(manifest["release"]).encode(),
    }
    events, volumes = [], {}
    crash = {"at": None}
    fence = {"active": True}
    database = Mock(id="db", labels={**labels, "com.docker.compose.service": "db"})
    database.running, database.exists = False, False

    def event(name):
        events.append(name)
        if crash["at"] == name:
            crash["at"] = None
            raise RuntimeError("Interrupted at " + name)

    def containers(**kwargs):
        if kwargs.get("filters", {}).get("volume") == "state":
            return []
        if "io.talos.agent" in kwargs.get("filters", {}).get("label", []):
            return []
        return [database] if database.exists and (kwargs.get("all") or database.running) else []

    def volume_get(name):
        if name not in volumes:
            raise NotFound("missing")
        return volumes[name]

    def volume_create(*, name, labels):
        event("create-" + name)
        volume = SimpleNamespace(attrs={"Labels": labels}, remove=lambda: volumes.pop(name))
        volumes[name] = volume
        return volume

    database.stop.side_effect = lambda **kw: setattr(database, "running", False)
    database.remove.side_effect = lambda: setattr(database, "exists", False)
    client = Mock()
    client.info.return_value = {"Architecture": "aarch64"}
    client.containers.list.side_effect = containers
    client.networks.list.return_value = []
    client.volumes.list.return_value = []
    client.volumes.get.side_effect = volume_get
    client.volumes.create.side_effect = volume_create

    @contextmanager
    def read(*args):
        yield manifest, tmp_path, configs.copy()

    @contextmanager
    def decrypt(*args):
        yield io.BytesIO(b"snapshot bytes")

    def compose(*args, **kwargs):
        if args[-1] == "db":
            database.exists, database.running = True, True
            event("database-start")
        else:
            event("services-start")

    def control(_, action, *args):
        if action == "leave":
            fence["active"] = False
        event(action)
        return {"maintenance": {"active": fence["active"]}}

    def command(args, **kwargs):
        event("database-import" if "pg_restore" in args else "volume-import")

    original_write = Installation.write
    original_atomic_text = snapshots.atomic_text

    def atomic_text(path, *args):
        original_atomic_text(path, *args)
        if path.name == ".env":
            event("config-write")

    def write(self, name, value):
        original_write(self, name, value)
        if name == "operation.json" and value["phase"] == "reopening":
            event("reopening")

    monkeypatch.setattr(snapshots, "read_backup", read)
    monkeypatch.setattr(snapshots, "atomic_text", atomic_text)
    monkeypatch.setattr(snapshots, "_decrypt", decrypt)
    monkeypatch.setattr(snapshots, "_database", lambda *args: "db")
    monkeypatch.setattr(snapshots, "_control", control)
    monkeypatch.setattr(snapshots, "_run", command)
    monkeypatch.setattr(snapshots.docker, "from_env", lambda: client)
    monkeypatch.setattr(Installation, "preflight_resources", lambda self: None)
    monkeypatch.setattr(Installation, "compose", lambda self, *args, **kw: compose(*args, **kw))
    monkeypatch.setattr(Installation, "ready", lambda self: event("ready"))
    monkeypatch.setattr(Installation, "write", write)

    def restore():
        return snapshots.restore(
            Installation(destination),
            archive,
            identity,
            source_fenced=True,
            destination_options={
                "owner_uid": os.getuid(),
                "owner_gid": os.getgid(),
                "docker_socket": "/new/socket",
                "host_os": "macos",
            },
        )

    return SimpleNamespace(
        directory=destination,
        archive=archive,
        crash=crash,
        events=events,
        restore=restore,
        installation=lambda: Installation(destination),
        volumes=volumes,
        database=database,
        client=client,
    )


@pytest.mark.parametrize(
    "point",
    [
        "config-write",
        "create-database",
        "volume-import",
        "database-start",
        "database-import",
        "sanitize",
        "reopening",
    ],
)
def test_cold_restore_retries_partial_import_without_adopting_other_data(cold_restore, point):
    cold_restore.crash["at"] = point
    with pytest.raises(RuntimeError, match="Interrupted"):
        cold_restore.restore()
    prior = cold_restore.installation().read("operation.json")
    assert prior["kind"] == "restore"
    assert prior["archive_sha256"] and prior["snapshot_operation_id"] == "snapshot-owner"
    if point == "config-write":
        assert not (cold_restore.directory / "bundle/manifest.json").exists()
    completed = cold_restore.restore()
    assert completed == "snapshot-owner"
    assert cold_restore.installation().read("operation.json")["phase"] == "reopening"
    assert cold_restore.installation().state["docker_socket"] == "/new/socket"
    assert (cold_restore.directory / "bundle/talos").is_file()
    snapshots.resume(cold_restore.installation(), completed)
    assert not (cold_restore.directory / ".backup-operation.json").exists()
    if point == "reopening":
        assert cold_restore.events.count("database-import") == 1
    elif point != "config-write":
        assert cold_restore.events.count("create-database") == 2


def test_restore_retry_rejects_another_snapshot_before_touching_resources(cold_restore):
    cold_restore.crash["at"] = "volume-import"
    with pytest.raises(RuntimeError):
        cold_restore.restore()
    cold_restore.archive.write_bytes(b"different snapshot")
    before = cold_restore.events.copy()
    with pytest.raises(snapshots.BackupError, match="identical snapshot"):
        cold_restore.restore()
    assert cold_restore.events == before


@pytest.mark.parametrize("after_cleanup", [False, True])
def test_restore_retry_never_reimports_after_lost_reopening_response(cold_restore, after_cleanup):
    operation = cold_restore.restore()
    if after_cleanup:
        snapshots.resume(cold_restore.installation(), operation)
    else:
        cold_restore.crash["at"] = "leave"
        with pytest.raises(RuntimeError, match="leave"):
            snapshots.resume(cold_restore.installation(), operation)
    cold_restore.events.clear()
    operation = cold_restore.restore()
    snapshots.resume(cold_restore.installation(), operation)
    assert cold_restore.events == ["status", "ready"]


def test_restore_retry_refuses_running_writers(cold_restore):
    cold_restore.crash["at"] = "database-import"
    with pytest.raises(RuntimeError):
        cold_restore.restore()
    cold_restore.database.labels["com.docker.compose.service"] = "gateway"
    before = cold_restore.events.copy()
    with pytest.raises(snapshots.BackupError, match="writers started"):
        cold_restore.restore()
    assert cold_restore.events == before


def test_restore_retry_refuses_foreign_volume(cold_restore):
    cold_restore.crash["at"] = "database-import"
    with pytest.raises(RuntimeError):
        cold_restore.restore()
    cold_restore.volumes["state"].attrs["Labels"] = {"io.talos.installation": "foreign"}
    before = cold_restore.events.copy()
    with pytest.raises(snapshots.BackupError, match="volume collision"):
        cold_restore.restore()
    assert cold_restore.events == before
    assert cold_restore.volumes["state"].attrs["Labels"]["io.talos.installation"] == "foreign"


@pytest.mark.parametrize("stop_fails", [False, True])
def test_reconciliation_stops_writers_before_resolving_and_preserves_retry_set(
    tmp_path,
    monkeypatch,
    stop_fails,
):
    from unittest.mock import Mock

    from backend.management.installation import Installation

    installation = Installation(tmp_path)
    installation.state = {"installation_id": "test"}
    client = Mock()
    running = [
        SimpleNamespace(labels={"com.docker.compose.service": name})
        for name in ("gateway", "connector")
    ]
    events = []
    fence = {"active": True}

    def compose(*args):
        events.append(args[0])
        if args[0] == "stop":
            if stop_fails:
                raise RuntimeError("Docker stop failed")
            running.clear()

    def control(_, action, *args):
        events.append(action)
        if action == "reconcile":
            assert not running
        if action == "leave":
            fence["active"] = False
        return {"maintenance": fence.copy()}

    monkeypatch.setattr(snapshots.docker, "from_env", lambda: client)
    monkeypatch.setattr(snapshots, "_stopped", lambda *args: None)
    monkeypatch.setattr(snapshots, "_containers", lambda *args, **kwargs: running.copy())
    monkeypatch.setattr(snapshots, "_control", control)
    installation.compose = compose
    installation.ready = lambda: events.append("ready")
    if stop_fails:
        with pytest.raises(RuntimeError, match="stop failed"):
            snapshots.reconcile(installation)
        assert "reconcile" not in events and "leave" not in events
        assert installation.read(".backup-operation.json")["services"] == ["connector", "gateway"]
        stop_fails = False
    snapshots.reconcile(installation)
    assert events.index("stop") < events.index("reconcile") < events.index("leave")
    assert installation.read("operation.json")["phase"] == "complete"
    assert not installation.read(".backup-operation.json")
