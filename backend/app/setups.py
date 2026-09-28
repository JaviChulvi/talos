"""Portable, inert setup bundles. Import never installs or executes their contents."""

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from backend.app.config import get_settings
from backend.app.db import Database
from backend.app.models import RUNTIME_RELEASES, Setup, SetupRevision

router = APIRouter(prefix="/api/v1/setups", tags=["setups"])
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILES = 20_000
Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
Text = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[^\x00]*$")]
FieldName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ToolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
SENSITIVE_NAME = re.compile(
    r"(^|[_-])(token|key|secret|password|authorization|cookie)($|[_-])", re.I
)


class BundleError(ValueError):
    """A safe diagnostic that contains no supplied field values."""


class BundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(BundleModel):
    runtime_kind: Literal["openclaw", "hermes"]
    runtime_release: str
    architecture: Literal["amd64", "arm64"]
    node_major: int | None = Field(default=None, ge=18, le=100)
    python_version: str | None = Field(default=None, pattern=r"^3\.[0-9]{1,2}$")


class Skill(BundleModel):
    id: Slug
    name: Text
    path: str
    enabled: bool = True
    source: str | None = Field(default=None, max_length=500)


class ConnectionReference(BundleModel):
    slot: Slug
    field: FieldName


class ConnectionSlot(BundleModel):
    id: Slug
    label: Text
    fields: list[FieldName] = Field(min_length=1, max_length=40)


class Connector(BundleModel):
    id: Slug
    name: Text
    enabled: bool = True
    source: str | None = Field(default=None, max_length=500)
    transport: Literal["stdio", "streamable-http", "sse"]
    tools: list[ToolName] = Field(default_factory=list, max_length=500)
    url: str | None = None
    runner: Literal["node", "python3"] | None = None
    entrypoint: str | None = None
    args: list[str] = Field(default_factory=list, max_length=100)
    env: dict[FieldName, str | ConnectionReference] = Field(default_factory=dict)
    headers: dict[str, str | ConnectionReference] = Field(default_factory=dict)
    provenance: str | None = Field(default=None, max_length=4000)


class Manifest(BundleModel):
    schema_version: Literal[1]
    instructions: str = Field(default="", max_length=250_000)
    targets: list[Target] = Field(min_length=1, max_length=8)
    skills: list[Skill] = Field(default_factory=list, max_length=200)
    connectors: list[Connector] = Field(default_factory=list, max_length=100)
    connection_slots: list[ConnectionSlot] = Field(default_factory=list, max_length=100)
    assets: dict[str, Hash] = Field(default_factory=dict)
    executables: list[str] = Field(default_factory=list, max_length=MAX_FILES)
    unresolved: list[dict] = Field(default_factory=list, max_length=500)


def empty_manifest() -> dict:
    return {
        "schema_version": 1,
        "instructions": "",
        "targets": [],
        "skills": [],
        "connectors": [],
        "connection_slots": [],
        "assets": {},
        "unresolved": [],
    }


def safe_path(value: str) -> str:
    if (
        not value
        or len(value) > 1000
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise BundleError("Bundle contains an unsafe relative path")
    return value


def _asset_hashes(files: dict[str, bytes]) -> dict[str, str]:
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise BundleError("Bundle exceeds its file count or total size limit")
    hashes = {}
    for path, content in files.items():
        safe_path(path)
        if path == "manifest.json" or len(content) > MAX_FILE_BYTES:
            raise BundleError("Asset uses a reserved name or exceeds the file size limit")
        if any(str(parent) in files for parent in PurePosixPath(path).parents):
            raise BundleError("Bundle file paths collide")
        hashes[path] = hashlib.sha256(content).hexdigest()
    return hashes


def _manifest_bytes(manifest: dict) -> bytes:
    try:
        encoded = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (ValueError, TypeError, RecursionError):
        raise BundleError("Manifest must contain valid JSON values") from None
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise BundleError("Manifest exceeds its size limit")
    return encoded


def validate_manifest(manifest: dict, files: dict[str, bytes], *, publication: bool = True) -> dict:
    """Validate assets in drafts; require a complete portable declaration when publishing."""
    if not isinstance(manifest, dict):
        raise BundleError("Manifest must be a JSON object")
    _manifest_bytes(manifest)
    if manifest.get("schema_version") != 1:
        raise BundleError("Unsupported setup schema version")
    if manifest.get("assets", {}) != _asset_hashes(files):
        raise BundleError("Manifest asset hashes do not match the bundle files")
    executables = manifest.get("executables", [])
    if (
        not isinstance(executables, list)
        or any(not isinstance(path, str) or path not in files for path in executables)
        or len(executables) != len(set(executables))
    ):
        raise BundleError("Executable assets must be unique declared file paths")
    if not publication:
        return manifest
    try:
        parsed = Manifest.model_validate(manifest)
    except ValidationError as error:
        locations = [".".join(map(str, item["loc"])) for item in error.errors()[:10]]
        raise BundleError("Invalid manifest fields: " + ", ".join(locations)) from None
    if parsed.unresolved:
        raise BundleError("Resolve or remove captured blockers before publication")
    targets = set()
    for target in parsed.targets:
        if target.runtime_release != RUNTIME_RELEASES[target.runtime_kind]:
            raise BundleError("Target runtime release is not supported by this Talos installation")
        key = (target.runtime_kind, target.architecture)
        if key in targets:
            raise BundleError("Duplicate runtime target")
        targets.add(key)
    slots = {slot.id: slot for slot in parsed.connection_slots}
    for items in (parsed.skills, parsed.connectors, parsed.connection_slots):
        if len({item.id for item in items}) != len(items):
            raise BundleError("Duplicate item identifiers")
    for slot in slots.values():
        if len(slot.fields) != len(set(slot.fields)):
            raise BundleError("Duplicate connection fields")
    roots = []
    for skill in parsed.skills:
        path = safe_path(skill.path)
        if not path.startswith("skills/"):
            raise BundleError("Skill directories must be under skills/")
        if any(
            path == root or path.startswith(root + "/") or root.startswith(path + "/")
            for root in roots
        ):
            raise BundleError("Skill directories overlap")
        roots.append(path)
        if skill.enabled:
            content = files.get(f"{path}/SKILL.md")
            if content is None:
                raise BundleError("Enabled skill is missing SKILL.md")
            try:
                lines = content.decode("utf-8").splitlines()
                if not lines or lines[0].strip() != "---":
                    raise ValueError
                end = next(
                    index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"
                )
                frontmatter = "\n".join(lines[1:end])
                if len(frontmatter) > 16_384:
                    raise ValueError
                metadata = yaml.safe_load(frontmatter)
                if not isinstance(metadata, dict) or metadata.get("name") != skill.name:
                    raise ValueError
            except (ValueError, UnicodeError, StopIteration, yaml.YAMLError):
                raise BundleError(
                    "SKILL.md frontmatter name must match its native skill name"
                ) from None
    enabled_names = [skill.name for skill in parsed.skills if skill.enabled]
    if len(enabled_names) != len(set(enabled_names)):
        raise BundleError("Enabled native skill names must be unique")
    for connector in parsed.connectors:
        roots.append(f"connectors/{connector.id}")
        if len(connector.tools) != len(set(connector.tools)):
            raise BundleError("Duplicate connector tool names")
        for mapping in (connector.env, connector.headers):
            for key, value in mapping.items():
                if any(char in key for char in "\r\n\x00"):
                    raise BundleError("Invalid connector setting name")
                if isinstance(value, ConnectionReference):
                    if value.slot not in slots or value.field not in slots[value.slot].fields:
                        raise BundleError("Connector references an undeclared connection field")
                elif "\x00" in value or "\n" in value or "\r" in value:
                    raise BundleError("Connector settings cannot contain line breaks")
                elif value and SENSITIVE_NAME.search(key):
                    raise BundleError("Credential settings must use a connection reference")
        if not connector.enabled:
            continue
        if not connector.tools:
            raise BundleError("Enabled connectors must declare their exposed tool names")
        if connector.transport == "stdio":
            if connector.runner is None or not connector.entrypoint or not connector.provenance:
                raise BundleError("Local connectors require a runner, entrypoint and provenance")
            entrypoint = safe_path(connector.entrypoint)
            if not entrypoint.startswith(f"connectors/{connector.id}/") or entrypoint not in files:
                raise BundleError("Local connector entrypoint must be included in its payload")
            if connector.url or connector.headers:
                raise BundleError("Local connectors cannot declare a URL or HTTP headers")
            for target in parsed.targets:
                if connector.runner == "node" and target.node_major is None:
                    raise BundleError(
                        "Local Node connectors require a pinned Node major per target"
                    )
                if connector.runner == "python3" and target.python_version is None:
                    raise BundleError("Local Python connectors require a pinned Python per target")
            if any("\x00" in arg for arg in connector.args):
                raise BundleError("Local connector arguments cannot contain null bytes")
        else:
            try:
                url = urlsplit(connector.url or "")
                valid = url.scheme in {"https", "http"} and bool(url.hostname)
                valid = valid and not (url.username or url.password or url.query or url.fragment)
                valid = valid and not any(char.isspace() for char in connector.url or "")
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise BundleError(
                    "Hosted connectors require an HTTP URL without embedded credentials"
                )
            if connector.runner or connector.entrypoint or connector.args or connector.env:
                raise BundleError("Hosted connectors cannot declare local process settings")
    if any(not any(path.startswith(root + "/") for root in roots) for path in files):
        raise BundleError("Every asset must belong to a declared skill or connector payload")
    return parsed.model_dump(exclude_none=True)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Manifest contains duplicate JSON keys")
        result[key] = value
    return result


def read_bundle(content: bytes, *, publication: bool = False) -> tuple[dict, dict[str, bytes]]:
    if len(content) > MAX_ARCHIVE_BYTES:
        raise BundleError("Bundle exceeds its upload size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES + 1:
                raise BundleError("Bundle exceeds its file count limit")
            seen, total, executables = set(), 0, []
            for entry in entries:
                path = safe_path(
                    entry.filename.removesuffix("/") if entry.is_dir() else entry.filename
                )
                if path in seen:
                    raise BundleError("Bundle contains duplicate paths")
                seen.add(path)
                mode = entry.external_attr >> 16
                expected = stat.S_IFDIR if entry.is_dir() else stat.S_IFREG
                if stat.S_IFMT(mode) not in {0, expected}:
                    raise BundleError("Bundle links and special files are not supported")
                if not entry.is_dir() and path != "manifest.json" and mode & 0o111:
                    executables.append(path)
                if entry.flag_bits & 1 or entry.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise BundleError("Encrypted or unsupported ZIP entries are not supported")
                limit = MAX_MANIFEST_BYTES if path == "manifest.json" else MAX_FILE_BYTES
                if entry.file_size > limit:
                    raise BundleError("Bundle entry exceeds its size limit")
                total += entry.file_size
                if total > MAX_TOTAL_BYTES:
                    raise BundleError("Bundle exceeds its total size limit")
            if "manifest.json" not in seen:
                raise BundleError("Bundle is missing manifest.json")
            manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=_json_object)
            files = {
                entry.filename: archive.read(entry)
                for entry in entries
                if not entry.is_dir() and entry.filename != "manifest.json"
            }
    except (
        zipfile.BadZipFile,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        NotImplementedError,
        RecursionError,
    ):
        raise BundleError("Bundle is not a valid supported ZIP with a JSON manifest") from None
    # ZIP permissions are imported as portable metadata, never applied to the host.
    if isinstance(manifest, dict):
        declared = manifest.get("executables", executables)
        if (
            not isinstance(declared, list)
            or any(not isinstance(path, str) for path in declared)
            or sorted(declared) != sorted(executables)
        ):
            raise BundleError("Manifest executable assets do not match ZIP permissions")
        if executables:
            manifest = {**manifest, "executables": sorted(executables)}
    return validate_manifest(manifest, files, publication=publication), files


def write_bundle(manifest: dict, files: dict[str, bytes]) -> bytes:
    """Rebuild a canonical ZIP. Assets are plain bytes, never filesystem paths to follow."""
    normalized = {**manifest, "assets": _asset_hashes(files)}
    validate_manifest(normalized, files, publication=False)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(
            {"manifest.json": _manifest_bytes(normalized), **files}.items()
        ):
            entry = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if path in normalized.get("executables", []) else 0o644
            entry.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(entry, content)
    content = output.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise BundleError("Bundle exceeds its archive size limit")
    return content


def store_bundle(content: bytes) -> str:
    """Atomically store immutable, content-addressed bytes before committing references."""
    read_bundle(content)
    digest = hashlib.sha256(content).hexdigest()
    directory = get_settings().setup_artifacts_dir
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{digest}.zip"
    fd, temp = tempfile.mkstemp(prefix=".bundle-", dir=directory)
    try:
        # Shared API/worker volume: readable by API uid 10001 and root worker.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return digest


def load_bundle(digest: str) -> tuple[dict, dict[str, bytes]]:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise BundleError("Invalid artifact reference")
    try:
        path = get_settings().setup_artifacts_dir / f"{digest}.zip"
        if path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise BundleError("Stored artifact exceeds its size limit")
        content = path.read_bytes()
    except OSError:
        raise BundleError("Setup artifact is unavailable") from None
    if hashlib.sha256(content).hexdigest() != digest:
        raise BundleError("Setup artifact integrity check failed")
    return read_bundle(content)


def save_draft(setup: Setup, manifest: dict, files: dict[str, bytes]) -> None:
    content = write_bundle(manifest, files)
    normalized, _ = read_bundle(content)
    setup.draft_artifact_hash = store_bundle(content)
    setup.draft_manifest = normalized


class SetupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    description: str = Field(default="", max_length=2000, pattern=r"^[^\x00]*$")


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict


class RevisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    setup_id: UUID
    version: int
    manifest: dict
    artifact_hash: str
    created_at: datetime


class SetupResponse(SetupInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    draft_manifest: dict
    draft_artifact_hash: str | None
    revisions: list[RevisionResponse]
    created_at: datetime
    updated_at: datetime


def _setup(session, setup_id, *, lock=False):
    setup = session.get(Setup, setup_id, with_for_update=lock)
    if setup is None:
        raise HTTPException(404, "Setup not found")
    return setup


def _draft_files(setup):
    return load_bundle(setup.draft_artifact_hash)[1] if setup.draft_artifact_hash else {}


def _bad_bundle(error):
    return HTTPException(422, str(error))


@router.get("", response_model=list[SetupResponse])
def list_setups(session: Database):
    return session.scalars(select(Setup).order_by(Setup.name, Setup.id)).all()


@router.post("", response_model=SetupResponse, status_code=201)
def create_setup(body: SetupInput, session: Database):
    setup = Setup(**body.model_dump(), draft_manifest=empty_manifest())
    session.add(setup)
    session.commit()
    return setup


@router.post("/import", response_model=SetupResponse, status_code=201)
async def import_setup(
    request: Request, session: Database, setup_id: UUID | None = None, name: str = "Imported setup"
):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise HTTPException(413, "Bundle exceeds its upload size limit")
    try:
        manifest, files = read_bundle(bytes(content))
        metadata = SetupInput(name=name)
        setup = _setup(session, setup_id, lock=True) if setup_id else Setup(**metadata.model_dump())
        save_draft(setup, manifest, files)
    except ValidationError:
        raise HTTPException(422, "Invalid setup name") from None
    except BundleError as error:
        raise _bad_bundle(error) from None
    session.add(setup)
    session.commit()
    return setup


@router.get("/{setup_id}", response_model=SetupResponse)
def get_setup(setup_id: UUID, session: Database):
    return _setup(session, setup_id)


@router.put("/{setup_id}", response_model=SetupResponse)
def update_setup(setup_id: UUID, body: SetupInput, session: Database):
    setup = _setup(session, setup_id, lock=True)
    for key, value in body.model_dump().items():
        setattr(setup, key, value)
    session.commit()
    return setup


@router.delete("/{setup_id}", status_code=204)
def delete_setup(setup_id: UUID, session: Database):
    setup = _setup(session, setup_id, lock=True)
    if setup.revisions:
        raise HTTPException(409, "Published setups are retained for pinned applications")
    session.delete(setup)
    session.commit()
    return Response(status_code=204)


@router.put("/{setup_id}/draft", response_model=SetupResponse)
def update_draft(setup_id: UUID, body: DraftInput, session: Database):
    setup = _setup(session, setup_id, lock=True)
    try:
        files = _draft_files(setup)
        # Editors may discard assets, but bytes can only be added/replaced through ZIP import.
        assets = body.manifest.get("assets", {})
        if not isinstance(assets, dict) or any(
            path not in files or digest != hashlib.sha256(files[path]).hexdigest()
            for path, digest in assets.items()
        ):
            raise BundleError("Draft asset edits can only remove existing assets")
        files = {path: content for path, content in files.items() if path in assets}
        validate_manifest(body.manifest, files, publication=False)
        save_draft(setup, body.manifest, files)
    except BundleError as error:
        raise _bad_bundle(error) from None
    session.commit()
    return setup


@router.get("/{setup_id}/draft/assets")
def inspect_asset(setup_id: UUID, path: str, session: Database):
    setup = _setup(session, setup_id)
    try:
        safe_path(path)
        files = _draft_files(setup)
    except BundleError as error:
        raise _bad_bundle(error) from None
    if path not in files:
        raise HTTPException(404, "Draft asset not found")
    return Response(
        files[path],
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{setup_id}/draft/export")
def export_draft(setup_id: UUID, session: Database):
    setup = _setup(session, setup_id)
    try:
        content = write_bundle(setup.draft_manifest, _draft_files(setup))
    except BundleError as error:
        raise _bad_bundle(error) from None
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="setup-{setup_id}-draft.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{setup_id}/validation")
def validate_draft(setup_id: UUID, session: Database):
    setup = _setup(session, setup_id)
    try:
        validate_manifest(setup.draft_manifest, _draft_files(setup))
    except BundleError as error:
        return {"valid": False, "errors": [str(error)]}
    return {"valid": True, "errors": []}


@router.post("/{setup_id}/revisions", response_model=RevisionResponse, status_code=201)
def publish_setup(setup_id: UUID, session: Database):
    setup = _setup(session, setup_id, lock=True)
    try:
        files = _draft_files(setup)
        manifest = validate_manifest(setup.draft_manifest, files)
        digest = store_bundle(write_bundle(manifest, files))
    except BundleError as error:
        raise _bad_bundle(error) from None
    version = (
        session.scalar(
            select(func.max(SetupRevision.version)).where(SetupRevision.setup_id == setup.id)
        )
        or 0
    )
    revision = SetupRevision(
        setup_id=setup.id, version=version + 1, manifest=manifest, artifact_hash=digest
    )
    session.add(revision)
    session.commit()
    return revision


@router.get("/{setup_id}/revisions/{revision_id}/export")
def export_setup(setup_id: UUID, revision_id: UUID, session: Database):
    _setup(session, setup_id)
    revision = session.get(SetupRevision, revision_id)
    if revision is None or revision.setup_id != setup_id:
        raise HTTPException(404, "Setup revision not found")
    try:
        manifest, files = load_bundle(revision.artifact_hash)
        content = write_bundle(manifest, files)
    except BundleError as error:
        raise HTTPException(409, str(error)) from None
    return Response(
        content,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="setup-{setup_id}-v{revision.version}.zip"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
