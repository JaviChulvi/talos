"""Organization connection metadata and write-only, immutable credential versions."""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.app.config import get_settings
from backend.app.db import Base, Database
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    Agent,
    Employee,
    EmployeeChannel,
    Operation,
    Role,
)

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (CheckConstraint("purpose IN ('tools','channel')"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    fields: Mapped[list] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(String(20), default="tools")
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("connection_versions.id", use_alter=True, name="fk_connection_current_version")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConnectionVersion(Base):
    __tablename__ = "connection_versions"
    __table_args__ = (UniqueConstraint("connection_id", "version"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    fields: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    description: str = Field(default="", max_length=2000, pattern=r"^[^\x00]*$")
    fields: list[str] = Field(min_length=1, max_length=40)

    @field_validator("fields")
    @classmethod
    def valid_fields(cls, value):
        if any(not _FIELD.fullmatch(field) for field in value) or len(set(value)) != len(value):
            raise ValueError(
                "Fields must be unique alphanumeric names beginning with a letter or underscore"
            )
        return sorted(value)


class ConnectionResponse(ConnectionInput):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    current_version: int
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class CredentialsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, SecretStr] = Field(min_length=1, max_length=40)

    @field_validator("values")
    @classmethod
    def valid_values(cls, values):
        # Error text intentionally does not interpolate either submitted key or value.
        if any(not _FIELD.fullmatch(key) for key in values):
            raise ValueError("Invalid credential field name")
        if any(
            not (1 <= len(value.get_secret_value()) <= 16384)
            or any(char in value.get_secret_value() for char in ("\x00", "\n", "\r"))
            for value in values.values()
        ):
            raise ValueError("Credential values must be nonempty single-line text")
        return values


class ConnectionBindingError(ValueError):
    """Safe to display; never contains credential contents."""


def _secret_directory() -> Path:
    return get_settings().connection_secrets_dir


def _secret_path(version_id: UUID | str) -> Path:
    return _secret_directory() / f"{UUID(str(version_id))}.json"


def write_credential_version(version_id: UUID, values: dict[str, str]) -> Path:
    """Publish a private file exactly once; finish durable storage before the DB commit."""
    directory = _secret_directory()
    directory.mkdir(mode=0o750, parents=True, exist_ok=True)
    if directory.stat().st_uid == os.geteuid():
        directory.chmod(0o750)
    path = _secret_path(version_id)
    descriptor, temporary = tempfile.mkstemp(prefix=".credential-", dir=directory)
    try:
        # The API owns files; its private group also contains the read-only worker.
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w") as target:
            json.dump(values, target, separators=(",", ":"), sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        # Linking an already private file is atomic and refuses to overwrite an old version.
        os.link(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def validate_connection_bindings(session: Session, bindings: dict | None) -> None:
    """Lock referenced rows while a role/employee binding is persisted."""
    for connection_id in sorted(set((bindings or {}).values())):
        try:
            identifier = UUID(str(connection_id))
        except (ValueError, TypeError, AttributeError):
            raise ConnectionBindingError("Connection binding is invalid") from None
        connection = session.get(Connection, identifier, with_for_update=True)
        if connection is None:
            raise ConnectionBindingError("Connection binding does not exist")
        if connection.purpose != "tools":
            raise ConnectionBindingError("Employee channel credentials cannot be given to agents")


def resolve_bindings(
    session: Session,
    manifest: dict,
    role_bindings: dict | None,
    employee_overrides: dict | None,
    *,
    lock: bool = True,
) -> dict:
    """Snapshot exact versions for requested slots; an invalid override never falls back."""
    resolved = {}
    defaults, overrides = role_bindings or {}, employee_overrides or {}
    slots = sorted(manifest.get("connection_slots", []), key=lambda item: item["id"])
    identifiers = {}
    for slot in slots:
        slot_id = slot["id"]
        identifier = overrides[slot_id] if slot_id in overrides else defaults.get(slot_id)
        if not identifier:
            raise ConnectionBindingError(f"Connection required for slot {slot_id}")
        try:
            identifiers[slot_id] = UUID(str(identifier))
        except (ValueError, TypeError, AttributeError):
            raise ConnectionBindingError(f"Invalid connection for slot {slot_id}") from None
    # Consistent lock ordering also covers two agents with reversed employee overrides.
    locked = {
        identifier: session.get(
            Connection, identifier, with_for_update=lock, populate_existing=True
        )
        for identifier in sorted(set(identifiers.values()))
    }
    for slot in slots:
        slot_id = slot["id"]
        connection = locked[identifiers[slot_id]]
        if connection is None or connection.current_version_id is None:
            raise ConnectionBindingError(f"Credentials required for slot {slot_id}")
        if connection.purpose != "tools":
            raise ConnectionBindingError("Employee channel credentials cannot be given to agents")
        version = session.get(ConnectionVersion, connection.current_version_id)
        required_fields = sorted(set(slot["fields"]))
        if (
            version is None
            or version.connection_id != connection.id
            or set(required_fields) - set(version.fields)
        ):
            raise ConnectionBindingError(f"Credential fields missing for slot {slot_id}")
        binding = {
            "connection_id": str(connection.id),
            "version_id": str(version.id),
            "fields": required_fields,
        }
        # Validate durable file availability before any worker stops the current agent.
        load_bound_secrets({slot_id: binding})
        resolved[slot_id] = binding
    return resolved


def load_bound_secrets(bindings: dict | None) -> dict[str, dict[str, str]]:
    """Read only selected fields from immutable files; the caller must not log the result."""
    result = {}
    for slot_id, binding in (bindings or {}).items():
        try:
            path = _secret_path(binding["version_id"])
            # Refuse symlinked replacement credential files.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as source:
                values = json.load(source)
            selected = {field: values[field] for field in binding["fields"]}
            if any(not isinstance(value, str) or not value for value in selected.values()):
                raise ValueError("Invalid credential contents")
        except (OSError, ValueError, TypeError, KeyError):
            raise ConnectionBindingError(f"Credentials unavailable for slot {slot_id}") from None
        result[slot_id] = selected
    return result


def _references(value, identifier: str) -> bool:
    if isinstance(value, dict):
        return any(_references(item, identifier) for item in value.values())
    if isinstance(value, list):
        return any(_references(item, identifier) for item in value)
    return str(value) == identifier


def connection_is_referenced(session: Session, connection_id: UUID) -> bool:
    if session.scalar(
        select(EmployeeChannel.id).where(EmployeeChannel.connection_id == connection_id)
    ):
        return True
    identifier = str(connection_id)
    # JSON ownership is intentionally inspected in Python for portable backend semantics.
    # All writers of these fields hold the connection row lock until commit.
    for model, field in (
        (Role, "connection_bindings"),
        (Employee, "connection_overrides"),
        (Agent, "selected_application"),
        (Agent, "applied_application"),
    ):
        column = getattr(model, field, None)
        if column is None:
            continue
        references = select(column)
        if model is Agent:
            # Tombstones retain audit snapshots after their native state has been removed.
            # A requested deletion still holds credentials until the worker completes it.
            references = references.where(Agent.observed_state != "deleted")
        if any(_references(value, identifier) for value in session.scalars(references)):
            return True
    return any(
        _references(value, identifier)
        for value in session.scalars(
            select(Operation.role_application).where(
                Operation.status.in_(ACTIVE_OPERATION_STATUSES)
            )
        )
    )


def _connection(session: Session, identifier: UUID, *, lock=False) -> Connection:
    connection = session.get(Connection, identifier, with_for_update=lock)
    if connection is None:
        raise HTTPException(404, "Connection not found")
    return connection


def _commit_metadata(session: Session):
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "A connection with that name already exists") from None


@router.get("", response_model=list[ConnectionResponse])
def connections(session: Database):
    return session.scalars(select(Connection).order_by(Connection.name, Connection.id)).all()


@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(body: ConnectionInput, session: Database):
    connection = Connection(**body.model_dump())
    session.add(connection)
    _commit_metadata(session)
    return connection


@router.get("/{connection_id}", response_model=ConnectionResponse)
def get_connection(connection_id: UUID, session: Database):
    return _connection(session, connection_id)


@router.put("/{connection_id}", response_model=ConnectionResponse)
def update_connection(connection_id: UUID, body: ConnectionInput, session: Database):
    connection = _connection(session, connection_id, lock=True)
    if connection.purpose == "channel" and body.fields != connection.fields:
        raise HTTPException(409, "Employee channel credential fields cannot change")
    if body.fields != connection.fields and connection.current_version_id is not None:
        raise HTTPException(409, "Credential fields cannot change after credentials are stored")
    for key, value in body.model_dump().items():
        setattr(connection, key, value)
    _commit_metadata(session)
    return connection


@router.put("/{connection_id}/credentials", response_model=ConnectionResponse)
def rotate_credentials(connection_id: UUID, body: CredentialsInput, session: Database):
    connection = _connection(session, connection_id, lock=True)
    if set(body.values) != set(connection.fields):
        raise HTTPException(422, "Credential fields must match the connection fields exactly")
    version = ConnectionVersion(
        id=uuid4(),
        connection_id=connection.id,
        version=connection.current_version + 1,
        fields=connection.fields,
    )
    try:
        write_credential_version(
            version.id, {key: value.get_secret_value() for key, value in body.values.items()}
        )
        session.add(version)
        session.flush()
        connection.current_version = version.version
        connection.current_version_id = version.id
        if connection.purpose == "channel":
            channel = session.scalar(
                select(EmployeeChannel).where(EmployeeChannel.connection_id == connection.id)
            )
            if channel is not None:
                channel.revision += 1
                channel.verified_version_id = None
                channel.verified_at = None
                channel.identity = {}
        session.commit()
    except Exception:
        session.rollback()
        # Keep the immutable file after an uncertain commit outcome. Removing it could
        # invalidate a transaction that committed before the database connection failed.
        # Unreferenced files are harmless and can be removed during offline maintenance.
        # Do not leak filesystem, DB parameters, or submitted data through exception text.
        raise HTTPException(503, "Could not store credentials; retry the operation") from None
    return connection


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: UUID, session: Database):
    connection = _connection(session, connection_id, lock=True)
    if connection_is_referenced(session, connection.id):
        raise HTTPException(
            409, "Connection is referenced by a role, employee, or agent application"
        )
    versions = session.scalars(
        select(ConnectionVersion).where(ConnectionVersion.connection_id == connection.id)
    ).all()
    connection.current_version_id = None
    session.flush()
    for version in versions:
        session.delete(version)
    session.flush()
    session.delete(connection)
    session.commit()
    # Delete only files belonging to deleted versions. A cleanup failure does not re-enable them.
    for version in versions:
        try:
            _secret_path(version.id).unlink(missing_ok=True)
        except OSError:
            pass
    return Response(status_code=204)
