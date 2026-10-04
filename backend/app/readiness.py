"""Admin-requested checks. GET reads evidence; only owners execute probes."""

import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.app.agents import IdempotencyKey
from backend.app.availability import HEARTBEAT_TTL, record_check
from backend.app.channels import channel_row
from backend.app.connections import Connection
from backend.app.db import Database
from backend.app.diagnostics import DiagnosticRequest, RunResponse, admit_run
from backend.app.models import (
    Agent,
    ChannelCursor,
    ChannelInbox,
    ChannelOutbox,
    ChannelProbe,
    ServiceHeartbeat,
    UserAccess,
    UserChannel,
)

router = APIRouter(prefix="/api/v1", tags=["readiness"])


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["runtime", "connections", "model"]


@router.post("/agents/{agent_id}/checks", status_code=202, response_model=RunResponse)
def request_agent_check(agent_id: UUID, body: ProbeInput, key: IdempotencyKey, session: Database):
    with session.begin():
        identifier = "probe:" + hashlib.sha256(key.encode()).hexdigest()
        run = admit_run(
            session, agent_id, DiagnosticRequest(message=f"Availability: {body.kind}"), identifier
        )
        if run.source != "probe":
            run.source = "probe"
            run.session_key = f"agent:main:probe:{agent_id}:{run.id}"
            run.inference = {**run.inference, "probe": body.kind}
            agent = session.get(Agent, agent_id)
            if body.kind == "model" and agent.runtime_mode == "managed":
                settings = run.inference.get("settings", {})
                run.inference = {
                    **run.inference,
                    "settings": {
                        **settings,
                        "max_output_tokens": min(settings.get("max_output_tokens", 16), 16),
                    },
                }
            record_check(
                session,
                agent,
                body.kind,
                "checking",
                "probe_queued",
                fingerprint=run.availability_fingerprint,
            )
        return run


def probe_response(probe):
    return {
        key: getattr(probe, key)
        for key in ("id", "channel_id", "revision", "status", "code", "created_at", "completed_at")
    }


@router.post("/channels/{channel_id}/check", status_code=202)
def request_channel_check(channel_id: UUID, key: IdempotencyKey, session: Database):
    with session.begin():
        channel = channel_row(session, channel_id, lock=True)
        connection = session.get(Connection, channel.connection_id)
        previous = session.scalar(
            select(ChannelProbe).where(
                ChannelProbe.channel_id == channel_id,
                ChannelProbe.idempotency_key == key,
            )
        )
        if previous:
            if (
                previous.revision != channel.revision
                or previous.credential_version_id != connection.current_version_id
            ):
                raise HTTPException(409, "Channel configuration changed; request a new check")
            return probe_response(previous)
        if connection.current_version_id is None:
            raise HTTPException(409, "Configure channel credentials before checking")
        if session.scalar(
            select(ChannelProbe.id).where(
                ChannelProbe.channel_id == channel_id,
                ChannelProbe.status.in_(("queued", "running")),
            )
        ):
            raise HTTPException(409, "Channel already has an active check")
        probe = ChannelProbe(
            channel_id=channel_id,
            revision=channel.revision,
            credential_version_id=connection.current_version_id,
            idempotency_key=key,
        )
        session.add(probe)
        session.flush()
        return probe_response(probe)


@router.get("/channel-checks/{probe_id}")
def get_channel_check(probe_id: UUID, session: Database):
    probe = session.get(ChannelProbe, probe_id)
    if probe is None:
        raise HTTPException(404, "Channel check not found")
    return probe_response(probe)


def channel_status(session, channel: UserChannel, now=None):
    now = now or datetime.now(UTC)
    connection = session.get(Connection, channel.connection_id, populate_existing=True)
    cursor = session.get(ChannelCursor, channel.id, populate_existing=True)
    alive = session.get(ServiceHeartbeat, "connector", populate_existing=True)
    state, code = "unknown", "not_checked"
    if not channel.enabled:
        state, code = "not_applicable", "channel_disabled"
    elif not connection.current_version_id:
        state, code = "blocked", "credentials_missing"
    elif cursor:
        if (
            cursor.revision != channel.revision
            or cursor.credential_version_id != connection.current_version_id
        ):
            state, code = "stale", "channel_changed"
        elif not cursor.checked_at or cursor.checked_at + HEARTBEAT_TTL <= now:
            state, code = "stale", "transport_evidence_expired"
        elif not alive or alive.checked_at + HEARTBEAT_TTL <= now:
            state, code = "stale", "connector_unavailable"
        else:
            state, code = cursor.state, cursor.code
    return {
        "channel_id": channel.id,
        "provider": channel.provider,
        "state": state,
        "code": code,
        "checked_at": cursor.checked_at if cursor else None,
        "expires_at": cursor.checked_at + HEARTBEAT_TTL if cursor and cursor.checked_at else None,
        "action": "Configure credentials, enable the channel or check the connector",
    }


@router.get("/channels/{channel_id}/availability")
def get_channel_availability(channel_id: UUID, session: Database):
    return channel_status(session, channel_row(session, channel_id))


@router.get("/user-accesses/{access_id}/deliveries")
def list_deliveries(access_id: UUID, session: Database):
    if session.get(UserAccess, access_id) is None:
        raise HTTPException(404, "User access not found")
    return [
        {
            "id": outbox.id,
            "run_id": inbox.run_id,
            "state": outbox.state,
            "code": outbox.code,
            "accepted_parts": outbox.next_part,
            "total_parts": len(outbox.parts),
            "updated_at": outbox.updated_at,
        }
        for outbox, inbox in session.execute(
            select(ChannelOutbox, ChannelInbox)
            .join(ChannelInbox)
            .where(ChannelInbox.access_id == access_id)
            .order_by(ChannelInbox.created_at.desc())
            .limit(20)
        )
    ]
