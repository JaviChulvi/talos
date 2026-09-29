"""Admin-only channel setup and explicit employee identity approval."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.connections import Connection, CredentialsInput, rotate_credentials
from backend.app.db import Database
from backend.app.models import AccessInvitation, Agent, Employee, EmployeeAccess, EmployeeChannel

router = APIRouter(prefix="/api/v1", tags=["employee channels"])
CREDENTIAL_FIELDS = {"telegram": ["bot_token"], "slack": ["app_token", "bot_token"]}


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: Literal["telegram", "slack"]
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    workspace_id: str = Field(default="", pattern=r"^(T[A-Z0-9]{2,39})?$")

    @model_validator(mode="after")
    def workspace(self):
        if bool(self.workspace_id) != (self.provider == "slack"):
            raise ValueError("Slack requires a workspace ID; Telegram does not use one")
        return self


class ChannelChange(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    enabled: bool


class AccessInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel_id: UUID
    employee_id: UUID
    agent_id: UUID
    external_scope: str = Field(default="", max_length=40)
    external_user_id: str | None = Field(default=None, min_length=1, max_length=40)


def channel_response(session: Session, channel: EmployeeChannel) -> dict:
    from backend.app.readiness import channel_status

    connection = session.get(Connection, channel.connection_id)
    return {
        "id": channel.id,
        "provider": channel.provider,
        "name": channel.name,
        "enabled": channel.enabled,
        "revision": channel.revision,
        "workspace_id": channel.workspace_id,
        "connection_id": channel.connection_id,
        "credential_fields": CREDENTIAL_FIELDS[channel.provider],
        "credentials_configured": connection.current_version_id is not None,
        "credential_version_id": connection.current_version_id,
        "verified": bool(
            connection.current_version_id
            and connection.current_version_id == channel.verified_version_id
        ),
        "identity": channel.identity,
        "verified_at": channel.verified_at,
        "availability": channel_status(session, channel),
    }


def access_response(access: EmployeeAccess) -> dict:
    return {
        key: getattr(access, key)
        for key in (
            "id",
            "channel_id",
            "employee_id",
            "agent_id",
            "external_scope",
            "external_user_id",
            "state",
            "revision",
            "created_at",
        )
    }


def channel_row(session: Session, channel_id: UUID, *, lock=False) -> EmployeeChannel:
    channel = session.get(EmployeeChannel, channel_id)
    if channel is None:
        raise HTTPException(404, "Channel not found")
    if lock:
        # Credential rotation uses this same connection -> channel lock order.
        session.get(Connection, channel.connection_id, with_for_update=True)
        channel = session.get(
            EmployeeChannel, channel_id, with_for_update=True, populate_existing=True
        )
    return channel


def validate_identity(channel: EmployeeChannel, scope: str, user_id: str | None):
    if scope != channel.workspace_id:
        raise HTTPException(422, "Identity must belong to the channel workspace")
    if user_id is not None:
        pattern = r"[1-9][0-9]{0,19}" if channel.provider == "telegram" else r"[UW][A-Z0-9]{2,39}"
        if not re.fullmatch(pattern, user_id):
            raise HTTPException(422, "Use the stable platform user ID, not a name or email")


def assigned_agent(session: Session, employee_id: UUID, agent_id: UUID) -> Agent:
    agent = session.get(Agent, agent_id, with_for_update=True, populate_existing=True)
    if (
        agent is None
        or agent.observed_state == "deleted"
        or agent.desired_state == "deleted"
        or agent.runtime_mode != "native"
        or agent.employee_id != employee_id
    ):
        raise HTTPException(409, "Choose a native agent assigned to this employee")
    if session.get(Employee, employee_id) is None:
        raise HTTPException(404, "Employee not found")
    return agent


def commit(session: Session):
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Channel or employee identity already exists") from None


@router.get("/channels")
def list_channels(session: Database):
    return [
        channel_response(session, channel)
        for channel in session.scalars(select(EmployeeChannel).order_by(EmployeeChannel.provider))
    ]


@router.post("/channels", status_code=201)
def create_channel(body: ChannelInput, session: Database):
    connection_id = uuid4()
    connection = Connection(
        id=connection_id,
        name=f"Employee channel {body.provider} {connection_id.hex[:8]}",
        purpose="channel",
        fields=CREDENTIAL_FIELDS[body.provider],
    )
    session.add(connection)
    session.flush()
    channel = EmployeeChannel(**body.model_dump(), connection_id=connection.id)
    session.add(channel)
    commit(session)
    return channel_response(session, channel)


@router.put("/channels/{channel_id}")
def change_channel(channel_id: UUID, body: ChannelChange, session: Database):
    channel = channel_row(session, channel_id, lock=True)
    channel.name = body.name
    if channel.enabled != body.enabled:
        channel.enabled = body.enabled
        channel.revision += 1
    commit(session)
    return channel_response(session, channel)


@router.put("/channels/{channel_id}/credentials")
def channel_credentials(channel_id: UUID, body: CredentialsInput, session: Database):
    channel = channel_row(session, channel_id, lock=True)
    # Reuse immutable write-only versions; rotation also invalidates channel verification.
    rotate_credentials(channel.connection_id, body, session)
    return channel_response(session, channel)


@router.get("/employee-accesses")
def list_accesses(session: Database, agent_id: UUID | None = None):
    query = select(EmployeeAccess).order_by(EmployeeAccess.created_at, EmployeeAccess.id)
    if agent_id is not None:
        query = query.where(EmployeeAccess.agent_id == agent_id)
    return [access_response(access) for access in session.scalars(query)]


@router.post("/employee-accesses", status_code=201)
def create_access(body: AccessInput, session: Database):
    assigned_agent(session, body.employee_id, body.agent_id)
    channel = channel_row(session, body.channel_id)
    validate_identity(channel, body.external_scope, body.external_user_id)
    access = EmployeeAccess(**body.model_dump())
    session.add(access)
    commit(session)
    return access_response(access)


def access_row(session: Session, access_id: UUID, *, lock=True) -> EmployeeAccess:
    access = session.get(EmployeeAccess, access_id, with_for_update=lock, populate_existing=True)
    if access is None:
        raise HTTPException(404, "Employee access not found")
    return access


@router.put("/employee-accesses/{access_id}")
def change_access(access_id: UUID, body: AccessInput, session: Database):
    assigned_agent(session, body.employee_id, body.agent_id)
    access = access_row(session, access_id)
    if access.channel_id != body.channel_id or access.employee_id != body.employee_id:
        raise HTTPException(409, "Create a separate access for another channel or employee")
    channel = channel_row(session, body.channel_id)
    validate_identity(channel, body.external_scope, body.external_user_id)
    for key, value in body.model_dump().items():
        setattr(access, key, value)
    access.state = "pending"
    access.revision += 1
    commit(session)
    return access_response(access)


@router.post("/employee-accesses/{access_id}/approve")
def approve_access(access_id: UUID, session: Database):
    existing = access_row(session, access_id, lock=False)
    target = (existing.employee_id, existing.agent_id)
    assigned_agent(session, *target)
    access = access_row(session, access_id)
    if target != (access.employee_id, access.agent_id):
        raise HTTPException(409, "Access changed; review the employee and agent again")
    channel = channel_row(session, access.channel_id)
    validate_identity(channel, access.external_scope, access.external_user_id)
    if access.external_user_id is None:
        raise HTTPException(409, "Wait for the employee identity before approval")
    if access.state != "active":
        access.state = "active"
        access.revision += 1
    commit(session)
    return access_response(access)


@router.post("/employee-accesses/{access_id}/disable")
def disable_access(access_id: UUID, session: Database):
    access = access_row(session, access_id)
    if access.state != "disabled":
        access.state = "disabled"
        access.revision += 1
    commit(session)
    return access_response(access)


@router.post("/employee-accesses/{access_id}/invitation", status_code=201)
def invite(access_id: UUID, session: Database, response: Response):
    access = access_row(session, access_id)
    if access.state == "active":
        raise HTTPException(409, "Disable an active access before issuing a new invitation")
    now = datetime.now(UTC)
    session.execute(
        update(AccessInvitation)
        .where(AccessInvitation.access_id == access_id, AccessInvitation.consumed_at.is_(None))
        .values(consumed_at=now)
    )
    access.state = "pending"
    access.revision += 1
    token = secrets.token_urlsafe(24)
    invitation = AccessInvitation(
        access_id=access.id,
        access_revision=access.revision,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=now + timedelta(minutes=15),
    )
    session.add(invitation)
    commit(session)
    response.headers["Cache-Control"] = "no-store"
    return {"token": token, "expires_at": invitation.expires_at, "access_id": access.id}


def claim_invitation(
    session: Session,
    channel_id: UUID,
    token: str,
    user_id: str,
    scope: str,
    *,
    now: datetime | None = None,
) -> EmployeeAccess:
    """Called only by the trusted connector; claiming never grants conversational access."""
    now = now or datetime.now(UTC)
    identifier = session.scalar(
        select(AccessInvitation.access_id).where(
            AccessInvitation.token_hash == hashlib.sha256(token.encode()).hexdigest()
        )
    )
    if identifier is None:
        raise HTTPException(403, "Invitation is invalid or expired")
    access = access_row(session, identifier)
    invitation = session.scalar(
        select(AccessInvitation)
        .where(AccessInvitation.token_hash == hashlib.sha256(token.encode()).hexdigest())
        .with_for_update()
    )
    if (
        invitation.expires_at <= now
        or invitation.consumed_at is not None
        or invitation.access_revision != access.revision
        or access.channel_id != channel_id
        or access.state != "pending"
    ):
        raise HTTPException(403, "Invitation is invalid or expired")
    channel = channel_row(session, channel_id)
    validate_identity(channel, scope, user_id)
    if access.external_user_id is not None and access.external_user_id != user_id:
        raise HTTPException(403, "Invitation belongs to another identity")
    access.external_user_id = user_id
    access.external_scope = scope
    access.revision += 1
    invitation.consumed_at = now
    session.flush()
    return access


def authorized_access(
    session: Session,
    access: EmployeeAccess | None,
    agent: Agent,
    *,
    access_revision: int | None = None,
    channel_revision: int | None = None,
) -> bool:
    """Revalidate saved identity and destination before dispatch and before delivery."""
    if (
        access is None
        or access.state != "active"
        or access.external_user_id is None
        or access.agent_id != agent.id
        or access.employee_id != agent.employee_id
        or agent.runtime_mode != "native"
        or agent.desired_state == "deleted"
        or (access_revision is not None and access_revision != access.revision)
    ):
        return False
    channel = session.get(EmployeeChannel, access.channel_id, populate_existing=True)
    connection = session.get(Connection, channel.connection_id, populate_existing=True)
    return bool(
        channel.enabled
        and connection.current_version_id
        and channel.verified_version_id == connection.current_version_id
        and access.external_scope == channel.workspace_id
        and (channel_revision is None or channel.revision == channel_revision)
    )
