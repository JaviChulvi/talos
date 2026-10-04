"""Administrator-requested encrypted snapshots and fenced restores.

Staging contains ciphertext only. The outer tar is an envelope of age-encrypted
components; its encrypted manifest binds every component by size and SHA-256.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

import docker

from backend.management.installation import atomic_text
from backend.management.release import IMAGE_PATTERN

BACKUP_FORMAT = 1
CONFIG_FILES = (
    "installation.json",
    "manifest.json",
    "compose.yaml",
    ".env",
    "Caddyfile",
    "bundle/manifest.json",
    "bundle/talos",
    "bundle/release.env",
    "bundle/checksums.txt",
    "bundle/compose.release.yaml",
    "bundle/Caddyfile.template",
    "bundle/images-amd64.txt",
    "bundle/images-arm64.txt",
    "bundle/LICENSE",
    "bundle/NOTICE",
    "bundle/THIRD_PARTY_NOTICES.md",
    "bundle/license-texts.tar.gz",
    "bundle/licenses-amd64.tar.gz",
    "bundle/licenses-arm64.tar.gz",
)
SAFE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,254}\Z")


class BackupError(RuntimeError):
    pass


def _run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _outside(path, directory):
    if path.resolve().is_relative_to(directory.resolve()):
        raise BackupError("Backup and recovery identity must be outside the installation directory")


def _identity(identity, *, create=False):
    identity = Path(identity).absolute()
    if identity.is_symlink():
        raise BackupError("Recovery identity must not be a symlink")
    if not identity.exists() and create:
        identity.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                _run(["age-keygen"], stdout=output)
                output.flush()
                os.fsync(output.fileno())
            parent_fd = os.open(identity.parent, os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            identity.unlink(missing_ok=True)
            raise
    if not identity.is_file() or identity.stat().st_mode & 0o077:
        raise BackupError("Recovery identity must exist with mode 0600")
    return identity


def _encrypt(destination, identity, source):
    recipient = _run(["age-keygen", "-y", str(identity)], capture_output=True).stdout.strip()
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        process = subprocess.Popen(
            ["age", "-r", recipient.decode()],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.PIPE,
        )
        try:
            if hasattr(source, "read"):
                while chunk := source.read(1024 * 1024):
                    process.stdin.write(chunk)
            else:
                for chunk in source:
                    process.stdin.write(chunk)
            process.stdin.close()
            process.stdin = None
            _, error = process.communicate()
            if process.returncode:
                raise BackupError(f"Encryption failed: {error.decode(errors='replace')[:200]}")
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            process.kill()
            process.wait()
            raise


@contextmanager
def _decrypt(path, identity):
    process = subprocess.Popen(
        ["age", "-d", "-i", str(identity), str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield process.stdout
        # Always consume to EOF: age authenticates the last chunk on read.
        while process.stdout.read(1024 * 1024):
            pass
        _, error = process.communicate()
        if process.returncode:
            raise BackupError(f"Backup decryption failed: {error.decode(errors='replace')[:200]}")
    except BaseException:
        process.kill()
        process.wait()
        raise


def _encrypt_command(destination, identity, args):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errors)
        try:
            _encrypt(destination, identity, process.stdout)
            if process.wait():
                raise BackupError("Snapshot command failed; no completed backup was published")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def _control(installation, action, operation_id="", kind="backup", *, image=None):
    if image is None:
        result = installation.compose(
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--pull",
            "never",
            "api",
            "python",
            "-m",
            "backend.management.backup",
            "_control",
            action,
            operation_id,
            kind,
            capture=True,
        )
    else:
        # A failed target migration must not require that target's ORM to work.
        # Use the snapshot's platform and the stable maintenance table contract.
        code = (
            "import os,sys;os.environ['TALOS_DATABASE_PASSWORD']=os.environ['POSTGRES_PASSWORD'];"
            "from backend.management.backup import _main;"
            "sys.argv=['backup','_control',*sys.argv[1:]];_main()"
        )
        result = _run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                installation.state["compose_project"] + "_control",
                "--env-file",
                str(installation.directory / ".env"),
                "-e",
                "TALOS_DATABASE_URL=postgresql+psycopg://talos@db:5432/talos",
                "--entrypoint",
                "python",
                image,
                "-c",
                code,
                action,
                operation_id,
                kind,
            ],
            capture_output=True,
            text=True,
        )
    output = result.stdout if hasattr(result, "stdout") else result
    return json.loads(output or "{}")


def _labels(installation):
    return {
        "com.docker.compose.project": installation.state["compose_project"],
        "io.talos.installation": installation.state["installation_id"],
    }


def _containers(client, installation, *, running=False):
    expected = _labels(installation)
    rows = client.containers.list(
        all=not running,
        filters={"label": [f"com.docker.compose.project={expected['com.docker.compose.project']}"]},
    )
    for row in rows:
        if row.labels.get("io.talos.installation") != expected["io.talos.installation"]:
            raise BackupError("Foreign container collides with the installation project")
    return rows


def _database(client, installation):
    rows = [
        row
        for row in _containers(client, installation, running=True)
        if row.labels.get("com.docker.compose.service") == "db"
    ]
    if len(rows) != 1:
        raise BackupError("Expected exactly one running installation database")
    return rows[0].id


def _stopped(client, installation):
    rows = client.containers.list(
        filters={
            "label": [
                f"io.talos.installation={installation.state['installation_id']}",
                "io.talos.agent",
            ]
        }
    )
    if rows:
        raise BackupError("Stop every user runtime before backup or update")


def _inventory(client, installation, database):
    """Cross-check expected persistent resources, then include all owned volumes."""
    config = installation.compose("config", "--format", "json", capture=True)
    config = json.loads(config.stdout if hasattr(config, "stdout") else config)
    expected = _labels(installation)
    definitions = config.get("volumes", {})
    required = {
        value.get("name", f"{installation.state['compose_project']}_{key}")
        for key, value in definitions.items()
    }
    db_volumes = {
        mount["Name"]
        for row in _containers(client, installation)
        if row.id == database
        for mount in row.attrs.get("Mounts", [])
        if mount["Type"] == "volume"
    }
    rows = client.volumes.list(
        filters={"label": [f"io.talos.installation={installation.state['installation_id']}"]}
    )
    found = {row.name: row for row in rows}
    if required - found.keys():
        raise BackupError("A required installation volume is missing or has foreign ownership")
    volumes = []
    for name, volume in sorted(found.items()):
        labels = volume.attrs.get("Labels") or {}
        if not SAFE_NAME.fullmatch(name) or volume.attrs.get("Driver") != "local":
            raise BackupError("Only named local Docker volumes can be backed up")
        if volume.attrs.get("Options"):
            raise BackupError("Volume driver options are unsupported for backup")
        project = labels.get("com.docker.compose.project", labels.get("io.talos.project"))
        if project != expected["com.docker.compose.project"]:
            raise BackupError("Volume ownership does not match the installation project")
        consumers = client.containers.list(filters={"volume": name})
        if any(row.id != database for row in consumers):
            raise BackupError(
                "A container still uses a snapshot volume; maintenance remains active"
            )
        volumes.append({"name": name, "labels": labels, "database": name in db_volumes})
    if len(db_volumes) != 1:
        raise BackupError("Database must use one owned named volume")
    return volumes


def _archive_members(stream):
    """Validate before extraction, including links and symlink parent traversals."""
    names, links = set(), set()
    with tarfile.open(fileobj=stream, mode="r|*") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\x00" in member.name:
                raise BackupError("Unsafe path in backup archive")
            name = str(path)
            if name in names or any(str(parent) in links for parent in path.parents):
                raise BackupError("Duplicate or linked parent in backup archive")
            names.add(name)
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise BackupError("Device or unsupported entry in backup archive")
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute():
                    raise BackupError("Absolute link in backup archive")
                resolved = list(path.parent.parts if member.issym() else ())
                for part in target.parts:
                    if part == "..":
                        if not resolved:
                            raise BackupError("Link escapes backup volume")
                        resolved.pop()
                    elif part != ".":
                        resolved.append(part)
                links.add(name)
            if any(str(PurePosixPath(existing)).startswith(name + "/") for existing in names):
                if member.issym() or member.islnk():
                    raise BackupError("Archive link replaces an existing parent")
    return names


def _config_bytes(installation):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name in CONFIG_FILES:
            path = installation.directory / name
            if not path.exists():
                if name == "Caddyfile":
                    continue
                raise BackupError(f"Missing installation file: {name}")
            if not path.is_file() or path.is_symlink():
                raise BackupError("Installation files must be ordinary files")
            archive.add(path, arcname=name, recursive=False)
    output.seek(0)
    return output


def _journal(installation, *, create=False, operation_id=None, kind="backup"):
    path = installation.directory / ".backup-operation.json"
    if path.exists():
        data = json.loads(path.read_text())
        if operation_id and data["operation_id"] != operation_id:
            raise BackupError("An interrupted backup owns the maintenance fence; resume it first")
        return data
    data = {
        "operation_id": operation_id or str(uuid4()),
        "kind": kind,
    }
    if create:
        installation.write(path.name, data)
    return data


def quiesce(installation, client, operation_id, kind):
    """Persist admission fencing and the restart set before stopping any writer."""
    _stopped(client, installation)
    journal = _journal(installation, create=True, operation_id=operation_id, kind=kind)
    _control(installation, "enter", journal["operation_id"], journal["kind"])
    running = [
        row.labels["com.docker.compose.service"]
        for row in _containers(client, installation, running=True)
        if row.labels.get("com.docker.compose.service") not in (None, "db", "migrate")
    ]
    if "services" not in journal:
        journal["services"] = sorted(set(running))
        installation.write(".backup-operation.json", journal)
    if running:
        installation.compose("stop", "--timeout", "60", *running)
    _stopped(client, installation)
    if any(
        row.labels.get("com.docker.compose.service") not in ("db", "migrate")
        for row in _containers(client, installation, running=True)
    ):
        raise BackupError("A platform writer did not stop; maintenance remains active")
    return journal


def reconcile(installation):
    prior = installation.read("operation.json", {})
    if prior.get("kind") == "reconcile" and prior.get("phase") == "reopening":
        resume(installation, prior["operation_id"])
    else:
        operation = installation.journal("reconcile", "preflight")
        client = docker.from_env()
        try:
            quiesce(installation, client, operation["operation_id"], "reconcile")
            _control(installation, "reconcile", operation["operation_id"], "reconcile")
            installation.journal("reconcile", "reopening")
        finally:
            client.close()
        resume(installation, operation["operation_id"])
    installation.journal("reconcile", "complete")


def backup(
    installation,
    destination: Path,
    identity: Path,
    *,
    operation_id=None,
    keep_maintenance=False,
    kind="backup",
) -> Path:
    destination, identity = Path(destination).absolute(), Path(identity).absolute()
    _outside(destination, installation.directory)
    _outside(identity, installation.directory)
    if destination.is_symlink():
        raise BackupError("Backup destination must not be a symlink")
    if destination.exists():
        return _resume_published_backup(
            installation, destination, identity, operation_id, keep_maintenance, kind
        )
    create_identity = not identity.exists()
    identity = _identity(identity, create=True)
    if create_identity:
        os.chown(identity, installation.state["owner_uid"], installation.state["owner_gid"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    client = docker.from_env()
    try:
        journal = quiesce(installation, client, operation_id, kind)
        operation_id = journal["operation_id"]
        database = _database(client, installation)
        volumes = _inventory(client, installation, database)
        runtime = _control(installation, "inventory", operation_id, journal["kind"])
        present = {volume["name"] for volume in volumes}
        if set(runtime["volumes"]) - present:
            raise BackupError("A recorded user volume is missing; maintenance remains active")
        with tempfile.TemporaryDirectory(
            prefix=".talos-encrypted-", dir=destination.parent
        ) as temp:
            stage = Path(temp)
            _encrypt(stage / "config.age", identity, _config_bytes(installation))
            _encrypt_command(
                stage / "database.age",
                identity,
                ["docker", "exec", database, "pg_dump", "-U", "talos", "-d", "talos", "-Fc"],
            )
            helper = installation.manifest["images"][installation.state["platform"]]["management"]
            for index, volume in enumerate(volumes):
                if volume["database"]:
                    continue
                component = f"volume-{index}.age"
                volume["component"] = component
                _encrypt_command(
                    stage / component,
                    identity,
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0:0",
                        "--entrypoint",
                        "tar",
                        "--mount",
                        f"type=volume,src={volume['name']},dst=/data,readonly",
                        helper,
                        "--numeric-owner",
                        "-cpf",
                        "-",
                        "-C",
                        "/data",
                        ".",
                    ],
                )
                with _decrypt(stage / component, identity) as stream:
                    _archive_members(stream)
            images = []
            references = set(
                installation.manifest["images"][installation.state["platform"]].values()
            )
            for versions in installation.manifest["runtime_versions"][
                installation.state["platform"]
            ].values():
                references.update(versions.values())
            references_by_id = {}
            for ref in references:
                try:
                    references_by_id[client.images.get(ref).id] = ref
                except docker.errors.ImageNotFound:
                    continue
            for index, image_id in enumerate(sorted(set(runtime["images"]))):
                image = client.images.get(image_id)
                matching = references_by_id.get(image.id)
                if matching is None:
                    matching = next(
                        (
                            ref
                            for ref in sorted(image.attrs.get("RepoDigests") or [])
                            if re.fullmatch(IMAGE_PATTERN, ref)
                        ),
                        None,
                    )
                item = {"id": image.id, "reference": matching}
                if matching is None:
                    item["component"] = f"image-{index}.age"
                    _encrypt_command(
                        stage / item["component"], identity, ["docker", "image", "save", image.id]
                    )
                images.append(item)
            components = {
                path.name: {"sha256": _sha(path), "size": path.stat().st_size}
                for path in stage.iterdir()
            }
            manifest = {
                "schema_version": BACKUP_FORMAT,
                "created_at": datetime.now(UTC).isoformat(),
                "installation_id": installation.state["installation_id"],
                "compose_project": installation.state["compose_project"],
                "platform": installation.state["platform"],
                "release": installation.manifest,
                "operation_id": operation_id,
                "directory": str(installation.directory),
                "config_modes": {
                    name: (installation.directory / name).stat().st_mode & 0o777
                    for name in CONFIG_FILES
                    if (installation.directory / name).exists()
                },
                "services": journal["services"],
                "volumes": volumes,
                "images": images,
                "components": components,
            }
            _encrypt(stage / "manifest.age", identity, io.BytesIO(json.dumps(manifest).encode()))
            result = stage / "completed.tar"
            with tarfile.open(result, "w") as archive:
                for name in ["manifest.age", *sorted(components)]:
                    archive.add(stage / name, arcname=name, recursive=False)
            _publish_snapshot(
                result,
                destination,
                installation.state["owner_uid"],
                installation.state["owner_gid"],
            )
        if not keep_maintenance:
            resume(installation, operation_id)
        return destination
    finally:
        client.close()


def _publish_snapshot(result, destination, uid, gid):
    result.chmod(0o600)
    os.chown(result, uid, gid)
    with result.open("rb") as stream:
        os.fsync(stream.fileno())
    # Ownership is final before publication; existing destinations are never touched.
    os.link(result, destination)
    parent_fd = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _resume_published_backup(
    installation, destination, identity, operation_id, keep_maintenance, kind
):
    """Publication may finish just before the process dies; never overwrite that archive."""
    journal = installation.read(".backup-operation.json", {})
    operation_id = operation_id or journal.get("operation_id")
    if not operation_id:
        raise BackupError("Backup destination already exists")
    with read_backup(destination, identity) as (manifest, _, _configs):
        if (
            manifest["operation_id"] != operation_id
            or manifest["installation_id"] != installation.state["installation_id"]
            or manifest["release"] != installation.manifest
        ):
            raise BackupError("Backup destination belongs to another operation or release")
        status = _control(installation, "status")
        owner = status["maintenance"]["operation_id"]
        if owner and owner != operation_id:
            raise BackupError("Another operation owns the maintenance fence")
        if owner and not keep_maintenance:
            if not journal:
                installation.write(
                    ".backup-operation.json",
                    {
                        "operation_id": operation_id,
                        "kind": kind,
                        "services": manifest["services"],
                    },
                )
            resume(installation, operation_id)
        elif owner is None:
            finish_resume(installation, operation_id)
        return destination


def finish_resume(installation, operation_id):
    """Remove only this operation's journal after the database fence was released."""
    path = installation.directory / ".backup-operation.json"
    if not path.exists():
        return
    journal = json.loads(path.read_text())
    if journal.get("operation_id") != operation_id:
        raise BackupError("Maintenance journal belongs to another operation")
    path.unlink()


def resume(installation, operation_id=None, *, verify=True):
    """Explicitly resume/cancel a snapshot operation; interrupted work stays fenced."""
    journal = _journal(installation, operation_id=operation_id)
    if not _control(installation, "status")["maintenance"]["active"]:
        if verify:
            installation.ready()
        finish_resume(installation, journal["operation_id"])
        return
    _control(installation, "assert", journal["operation_id"], journal["kind"])
    # Start services under the fence; none can admit work until all startup calls succeed.
    if journal.get("services"):
        installation.compose(
            "up", "-d", "--no-deps", "--pull", "never", "--no-build", *journal["services"]
        )
    if verify:
        installation.ready()
    _control(installation, "leave", journal["operation_id"], journal["kind"])
    finish_resume(installation, journal["operation_id"])


@contextmanager
def read_backup(archive, identity):
    """Fully authenticate ciphertext and volume archive structure before mutation."""
    identity = _identity(identity)
    with tempfile.TemporaryDirectory(prefix="talos-encrypted-restore-") as temp:
        stage = Path(temp)
        with tarfile.open(archive, "r:") as envelope:
            names = set()
            for member in envelope:
                if (
                    not member.isfile()
                    or not re.fullmatch(
                        r"(?:manifest|config|database|volume-[0-9]+|image-[0-9]+)\.age", member.name
                    )
                    or member.name in names
                ):
                    raise BackupError("Invalid backup envelope")
                names.add(member.name)
                with (stage / member.name).open("xb") as output:
                    os.chmod(stage / member.name, 0o600)
                    source = envelope.extractfile(member)
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
        if "manifest.age" not in names:
            raise BackupError("Missing backup manifest")
        with _decrypt(stage / "manifest.age", identity) as stream:
            manifest = json.load(stream)
        if (
            type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != BACKUP_FORMAT
        ):
            raise BackupError("Unsupported backup format")
        components = manifest["components"]
        if set(components) | {"manifest.age"} != names:
            raise BackupError("Backup component inventory mismatch")
        for name, description in components.items():
            path = stage / name
            if path.stat().st_size != description["size"] or _sha(path) != description["sha256"]:
                raise BackupError("Backup component checksum mismatch")
            with _decrypt(path, identity) as stream:
                if name.startswith("volume-"):
                    _archive_members(stream)
                else:
                    while stream.read(1024 * 1024):
                        pass
        with _decrypt(stage / "config.age", identity) as stream:
            config_data = stream.read()
        configs = {}
        with tarfile.open(fileobj=io.BytesIO(config_data)) as config:
            for member in config:
                if not member.isfile() or member.name not in CONFIG_FILES or member.name in configs:
                    raise BackupError("Unsafe installation configuration archive")
                configs[member.name] = config.extractfile(member).read()
        if set(CONFIG_FILES) - {"Caddyfile"} - configs.keys():
            raise BackupError("Incomplete installation configuration")
        state = json.loads(configs["installation.json"])
        release = json.loads(configs["manifest.json"])
        if (
            state["installation_id"] != manifest["installation_id"]
            or state["compose_project"] != manifest["compose_project"]
            or state["platform"] != manifest["platform"]
            or release != manifest["release"]
        ):
            raise BackupError("Backup installation metadata mismatch")
        from backend.management.release import validate_manifest

        validate_manifest(release)
        used = {"config.age", "database.age"}
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", manifest["installation_id"]):
            raise BackupError("Invalid backup installation identity")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", manifest["compose_project"]):
            raise BackupError("Invalid backup project identity")
        volume_names = set()
        if sum(bool(row["database"]) for row in manifest["volumes"]) != 1:
            raise BackupError("Backup must contain exactly one database volume")
        for row in manifest["volumes"]:
            if row["name"] in volume_names or not SAFE_NAME.fullmatch(row["name"]):
                raise BackupError("Invalid or duplicate backup volume")
            volume_names.add(row["name"])
            if not row["database"]:
                component = row["component"]
                if not re.fullmatch(r"volume-[0-9]+\.age", component) or component in used:
                    raise BackupError("Invalid or duplicate volume component")
                used.add(component)
        for row in manifest["images"]:
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", row["id"]):
                raise BackupError("Invalid image identity")
            if row.get("reference") and not re.fullmatch(IMAGE_PATTERN, row["reference"]):
                raise BackupError("Backup image reference must be an immutable repository digest")
            if not row.get("reference"):
                component = row["component"]
                if not re.fullmatch(r"image-[0-9]+\.age", component) or component in used:
                    raise BackupError("Invalid or duplicate image component")
                used.add(component)
        if used != set(components):
            raise BackupError("Snapshot contents do not match the component inventory")
        yield manifest, stage, configs


def list_restore_images(archive: Path, identity: Path) -> list[str]:
    """Authenticate the snapshot before the host pulls its immutable image references.

    The caller uses its own Docker credential store; no registry credentials enter
    this management process. Saved local-only images are loaded during restore.
    """
    with read_backup(archive, identity) as (manifest, _, configs):
        platform = manifest["platform"]
        references = set(manifest["release"]["images"][platform].values())
        for versions in manifest["release"]["runtime_versions"][platform].values():
            references.update(versions.values())
        # Updates retain the installation's approved catalog, which may differ
        # from the current release even when no user uses an older image yet.
        from backend.app.runtime_versions import validate_catalog

        environment = dict(
            line.split("=", 1)
            for line in configs[".env"].decode().splitlines()
            if "=" in line and not line.startswith("#")
        )
        catalog = validate_catalog(json.loads(environment["TALOS_RUNTIME_VERSIONS"]))
        for versions in catalog.values():
            for reference in versions.values():
                if not re.fullmatch(IMAGE_PATTERN, reference):
                    raise BackupError("Restored runtime catalog requires pullable image digests")
                references.add(reference)
        references.update(row["reference"] for row in manifest["images"] if row.get("reference"))
        return sorted(references)


def _ready_existing_database(installation, client):
    """Recover the stop-before-checkpoint window without starting platform writers."""
    databases = [
        row
        for row in _containers(client, installation)
        if row.labels.get("com.docker.compose.service") == "db"
    ]
    if len(databases) != 1:
        raise BackupError(
            "Rollback requires the existing owned database until its fence is checked"
        )
    database = databases[0]
    database.reload()
    if database.status in ("exited", "created"):
        database.start()
    elif database.status != "running":
        raise BackupError("The existing rollback database is not startable")
    deadline = time.monotonic() + 60
    while True:
        database.reload()
        state = database.attrs.get("State", {})
        if state.get("Status") == "running" and state.get("Health", {}).get("Status") == "healthy":
            return
        if state.get("Status") not in ("running", "created", "restarting"):
            raise BackupError("The existing rollback database failed to start")
        if time.monotonic() >= deadline:
            raise BackupError("The existing rollback database did not become ready")
        time.sleep(0.25)


def _prepare_rollback(installation, client, manifest, archive, operation_id):
    """The persistent checkpoint permits retry after the database volume is replaced."""
    if (
        installation.state["installation_id"] != manifest["installation_id"]
        or not operation_id
        or operation_id != manifest["operation_id"]
    ):
        raise BackupError("Rollback requires the matching update snapshot and fence")
    if installation.read("operation.json", {}).get("phase") in (
        "reopening",
        "complete",
    ):
        raise BackupError("Traffic may have resumed; rollback is no longer allowed")
    checkpoint = installation.read(".restore-operation.json", {})
    expected = {
        "operation_id": operation_id,
        "archive_sha256": _sha(Path(archive)),
        "installation_id": manifest["installation_id"],
        "phase": "fenced",
    }
    if checkpoint:
        if checkpoint != expected:
            raise BackupError("Another restore operation owns this destination")
        # The journal was committed only after verifying the DB fence and
        # stopping every writer. It survives replacement of the DB volume.
        if any(
            row.labels.get("com.docker.compose.service") != "db"
            for row in _containers(client, installation, running=True)
        ):
            raise BackupError("Services resumed during rollback; stop and investigate")
        for database in _containers(client, installation, running=True):
            database.stop(timeout=60)
    else:
        # A previous attempt may have stopped the DB and died before persisting
        # its checkpoint. Restart only that owned DB, then prove fence ownership.
        _ready_existing_database(installation, client)
        _control(
            installation,
            "assert",
            operation_id,
            "update",
            image=manifest["release"]["images"][manifest["platform"]]["platform"],
        )
        installation.compose("stop", "--timeout", "60")
        _stopped(client, installation)
        if _containers(client, installation, running=True):
            raise BackupError("Stop all platform writers before restoring their volumes")
        installation.write(".restore-operation.json", expected)


def restore(
    installation,
    archive: Path,
    identity: Path,
    *,
    rollback=False,
    operation_id=None,
    source_fenced=False,
    destination_options=None,
):
    if not rollback and not source_fenced:
        raise BackupError("Confirm the original installation is stopped and fenced before restore")
    identity = _identity(identity)
    directory = installation.directory
    prior = installation.read("operation.json", {})
    retry_restore = (
        not rollback
        and prior.get("kind") == "restore"
        and prior.get("phase") in ("restore", "reopening")
    )
    controls = {"operation.lock", ".talos-lock", ".downloads"}
    if not rollback and not retry_restore and directory.exists():
        if any(path.name not in controls for path in directory.iterdir()):
            raise BackupError("Restore requires an empty installation directory")
    _outside(identity, directory)
    client = docker.from_env()
    try:
        with read_backup(archive, identity) as (manifest, stage, configs):
            restore_identity = (
                {
                    "archive_sha256": _sha(Path(archive)),
                    "snapshot_operation_id": manifest["operation_id"],
                    "installation_id": manifest["installation_id"],
                    "destination_options": destination_options,
                }
                if not rollback
                else {}
            )
            if retry_restore:
                if any(prior.get(key) != value for key, value in restore_identity.items()):
                    raise BackupError("Resume restore with the identical snapshot and host options")
                if prior["phase"] == "reopening":
                    # Traffic may already have resumed. Never import the snapshot again.
                    return manifest["operation_id"]
            machine = client.info()
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
                machine["Architecture"], machine["Architecture"]
            )
            if "linux/" + architecture != manifest["platform"]:
                raise BackupError("Cross-architecture restore is unsupported")
            # Name-based checks also catch unlabeled resources that Compose would
            # otherwise adopt; use the authenticated snapshot identity before writes.
            previous_state = installation.state
            try:
                installation.state = json.loads(configs["installation.json"])
                installation.preflight_resources()
            finally:
                installation.state = previous_state
            if rollback:
                _prepare_rollback(installation, client, manifest, archive, operation_id)
            for kind in (client.containers, client.networks, client.volumes):
                kwargs = {"all": True} if kind is client.containers else {}
                rows = kind.list(
                    **kwargs,
                    filters={"label": [f"io.talos.installation={manifest['installation_id']}"]},
                )
                project_rows = kind.list(
                    **kwargs,
                    filters={
                        "label": [f"com.docker.compose.project={manifest['compose_project']}"]
                    },
                )
                if (rows or project_rows) and not (rollback or retry_restore):
                    raise BackupError(
                        "The source installation or project exists on this Docker host"
                    )
            for volume in manifest["volumes"]:
                if not SAFE_NAME.fullmatch(volume["name"]):
                    raise BackupError("Invalid volume name")
                labels = volume["labels"]
                project = labels.get("com.docker.compose.project", labels.get("io.talos.project"))
                if (
                    labels.get("io.talos.installation") != manifest["installation_id"]
                    or project != manifest["compose_project"]
                ):
                    raise BackupError("Backup contains foreign volume ownership")
                try:
                    existing = client.volumes.get(volume["name"])
                except docker.errors.NotFound:
                    continue
                if not (rollback or retry_restore) or existing.attrs.get("Labels") != labels:
                    raise BackupError("Destination volume collision")
            # Every referenced image must already have been pulled by the host launcher.
            for image in manifest["images"]:
                if image.get("reference"):
                    if client.images.get(image["reference"]).id != image["id"]:
                        raise BackupError("Restored runtime image identity does not match")
                else:
                    with _decrypt(stage / image["component"], identity) as stream:
                        _run(["docker", "image", "load"], stdin=stream, stdout=subprocess.DEVNULL)
                    if client.images.get(image["id"]).id != image["id"]:
                        raise BackupError("Image archive did not restore the required identity")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            installation.state = json.loads(configs["installation.json"])
            if not rollback and destination_options:
                allowed = {"owner_uid", "owner_gid", "docker_socket", "host_os"}
                if set(destination_options) - allowed:
                    raise BackupError("Unsupported destination host options")
                installation.state.update(destination_options)
                env = configs[".env"].decode().splitlines()
                configs[".env"] = (
                    "\n".join(
                        "TALOS_DOCKER_SOCKET=" + installation.state["docker_socket"]
                        if line.startswith("TALOS_DOCKER_SOCKET=")
                        else line
                        for line in env
                    )
                    + "\n"
                ).encode()
            if not rollback:
                if retry_restore:
                    _stopped(client, installation)
                    running = _containers(client, installation, running=True)
                    if any(row.labels.get("com.docker.compose.service") != "db" for row in running):
                        raise BackupError(
                            "Platform writers started during restore; investigate first"
                        )
                    for database in running:
                        database.stop(timeout=60)
                # This durable identity precedes every destination file/volume write.
                installation.journal("restore", "restore", **restore_identity)
            old_caddy = str(Path(manifest["directory"]) / "Caddyfile")
            configs["compose.yaml"] = configs["compose.yaml"].replace(
                old_caddy.encode(), str(directory / "Caddyfile").encode()
            )
            # The launcher selects the saved bundle when its manifest exists.
            # Publish that marker last so a retry can still use the original bundle.
            for name in sorted(configs, key=lambda name: name == "bundle/manifest.json"):
                content = configs[name]
                path = directory / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chown(
                    path.parent, installation.state["owner_uid"], installation.state["owner_gid"]
                )
                atomic_text(
                    path,
                    content.decode(),
                    installation.state["owner_uid"],
                    installation.state["owner_gid"],
                )
                path.chmod(manifest["config_modes"].get(name, 0o600) & 0o777)
            installation.manifest = manifest["release"]
            helper = manifest["release"]["images"][manifest["platform"]]["management"]
            for volume in manifest["volumes"]:
                if rollback or retry_restore:
                    # Remove only already-verified owned volumes after stopping every consumer.
                    consumers = client.containers.list(all=True, filters={"volume": volume["name"]})
                    for container in consumers:
                        if (
                            container.labels.get("io.talos.installation")
                            != manifest["installation_id"]
                        ):
                            raise BackupError("Foreign container uses a restore volume")
                        container.remove()
                    try:
                        client.volumes.get(volume["name"]).remove()
                    except docker.errors.NotFound:
                        pass
                client.volumes.create(name=volume["name"], labels=volume["labels"])
                if volume["database"]:
                    continue
                with _decrypt(stage / volume["component"], identity) as stream:
                    _run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "-i",
                            "--network",
                            "none",
                            "--read-only",
                            "--user",
                            "0:0",
                            "--entrypoint",
                            "tar",
                            "--mount",
                            f"type=volume,src={volume['name']},dst=/data",
                            helper,
                            "--numeric-owner",
                            "-xpf",
                            "-",
                            "-C",
                            "/data",
                        ],
                        stdin=stream,
                    )
            installation.compose("up", "-d", "--wait", "--pull", "never", "--no-build", "db")
            database = _database(client, installation)
            with _decrypt(stage / "database.age", identity) as stream:
                _run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        database,
                        "pg_restore",
                        "-U",
                        "talos",
                        "-d",
                        "talos",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-acl",
                    ],
                    stdin=stream,
                )
            if not rollback:
                _control(installation, "sanitize", manifest["operation_id"], "backup")
            installation.write(
                ".backup-operation.json",
                {
                    "operation_id": manifest["operation_id"],
                    "kind": "update" if rollback else "backup",
                    "services": manifest["services"],
                },
            )
            installation.state["phase"] = "restored"
            installation.save()
            if rollback:
                (directory / ".restore-operation.json").unlink(missing_ok=True)
            else:
                installation.journal("restore", "reopening")
            # Leave all writers stopped and admission fenced. The caller verifies readiness,
            # then resumes explicitly; restore never reconnects users automatically.
            return manifest["operation_id"]
    finally:
        client.close()


def sanitize_restored_database(session):
    """Preserve user data while making restored credentials and queues inert."""
    from sqlalchemy import delete, select, update

    # The management subprocess does not import API routers. Register the target
    # tables before flushing new cursors with connection-version foreign keys.
    from backend.app import connections  # noqa: F401
    from backend.app.models import (
        ACTIVE_OPERATION_STATUSES,
        ACTIVE_RUN_STATUSES,
        AccessInvitation,
        AdministratorSession,
        Agent,
        AvailabilityCheck,
        ChannelCursor,
        ChannelOutbox,
        ChannelProbe,
        DeliveryChallenge,
        InferenceCall,
        Operation,
        Run,
        UserAccess,
        UserChannel,
        WorkloadIncarnation,
    )

    now = datetime.now(UTC)
    session.execute(delete(AdministratorSession))
    session.execute(delete(AccessInvitation))
    session.execute(delete(AvailabilityCheck))
    session.execute(
        update(Agent)
        .where(Agent.desired_state != "deleted")
        .values(
            desired_state="stopped",
            observed_state="stopped",
            current_incarnation_id=None,
            revision=Agent.revision + 1,
        )
    )
    session.execute(
        update(WorkloadIncarnation).values(
            revoked_at=now,
            expires_at=now,
            gateway_token_hash=None,
            container_id=None,
            container_name=None,
        )
    )
    session.execute(
        update(UserChannel).values(
            enabled=False,
            revision=UserChannel.revision + 1,
            verified_version_id=None,
            verified_at=None,
        )
    )
    session.execute(update(UserAccess).values(state="disabled", revision=UserAccess.revision + 1))
    session.execute(update(DeliveryChallenge).values(expires_at=now))
    session.execute(
        update(ChannelOutbox)
        .where(ChannelOutbox.state.in_(("waiting", "pending", "sending", "uncertain")))
        .values(state="blocked", code="restored_quarantine", retry_at=None)
    )
    session.execute(
        update(Operation)
        .where(Operation.status.in_(ACTIVE_OPERATION_STATUSES))
        .values(
            status="failed",
            step="restored_quarantine",
            owner=None,
            error="Restored; reconcile manually",
        )
    )
    session.execute(
        update(Run)
        .where(Run.status.in_(ACTIVE_RUN_STATUSES))
        .values(
            status="interrupted", error="Restored; external outcome must be reconciled manually"
        )
    )
    session.execute(
        update(InferenceCall).where(InferenceCall.completed_at.is_(None)).values(completed_at=now)
    )
    session.execute(
        update(ChannelProbe)
        .where(ChannelProbe.status.in_(("queued", "running")))
        .values(status="stale", code="restored_quarantine", completed_at=now)
    )
    for channel in session.scalars(select(UserChannel)):
        cursor = session.get(ChannelCursor, channel.id)
        if cursor is None:
            cursor = ChannelCursor(channel_id=channel.id)
            session.add(cursor)
        cursor.offset, cursor.state, cursor.code = 0, "restored", "restore_reconnect_required"
        cursor.reconnect_required, cursor.accept_after = True, None


def _main():
    import sys

    if len(sys.argv) == 4 and sys.argv[1] == "_images":
        for reference in list_restore_images(Path(sys.argv[2]), Path(sys.argv[3])):
            print(reference)
        return

    from sqlalchemy import select

    from backend.app.db import session_factory
    from backend.app.installation import (
        enter_maintenance,
        installation_status,
        leave_maintenance,
        reconcile_uncertain,
    )
    from backend.app.models import Agent

    _, _, action, operation_id, kind = sys.argv
    with session_factory()() as session:
        if action == "enter":
            enter_maintenance(session, operation_id, kind)
        elif action == "status":
            pass
        elif action in ("assert", "leave", "inventory", "sanitize", "reconcile"):
            status = installation_status(session)
            if status["maintenance"]["operation_id"] != operation_id:
                raise BackupError("Maintenance fence owner does not match")
            if action == "leave":
                leave_maintenance(session, operation_id)
            elif action == "reconcile":
                reconcile_uncertain(session, operation_id)
            elif action == "sanitize":
                sanitize_restored_database(session)
            elif action == "inventory":
                from backend.app.config import get_settings

                settings = get_settings()
                agents = list(
                    session.scalars(select(Agent).where(Agent.desired_state != "deleted"))
                )
                incarnations = [
                    agent.current_incarnation for agent in agents if agent.current_incarnation
                ]
                volumes = {row.config_volume for row in incarnations if row.config_volume}
                for agent_id in {row.agent_id for row in incarnations}:
                    volumes.add(
                        f"talos-{settings.compose_project}-{settings.installation_id}-{agent_id.hex}-state"
                    )
                images = {row.image_digest for row in incarnations if row.image_digest}
                images.update(agent.runtime_image for agent in agents if agent.runtime_image)
                print(json.dumps({"volumes": sorted(volumes), "images": sorted(images)}))
                return
        else:
            raise BackupError("Unknown maintenance command")
        session.commit()
        print(json.dumps(installation_status(session), default=str))


if __name__ == "__main__":
    _main()
