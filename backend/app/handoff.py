"""Delivery evidence is historical; current readiness is a separate contract."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select

from backend.app.applications import application_fingerprint
from backend.app.availability import agent_fingerprint
from backend.app.channels import access_row, assigned_agent, authorized_access, channel_row
from backend.app.connections import Connection
from backend.app.db import Database
from backend.app.models import (
    Agent,
    ChannelInbox,
    ChannelOutbox,
    DeliveryChallenge,
    EmployeeAccess,
    EmployeeChannel,
    Run,
)

router = APIRouter(prefix="/api/v1/employee-accesses", tags=["delivery verification"])


def current_challenge(session, challenge, access=None):
    access = access or session.get(EmployeeAccess, challenge.access_id, populate_existing=True)
    agent = session.get(Agent, challenge.agent_id, populate_existing=True)
    channel = session.get(EmployeeChannel, access.channel_id, populate_existing=True)
    connection = session.get(Connection, channel.connection_id, populate_existing=True)
    return bool(
        agent
        and authorized_access(
            session,
            access,
            agent,
            access_revision=challenge.access_revision,
            channel_revision=challenge.channel_revision,
        )
        and connection.current_version_id == challenge.credential_version_id
        and agent_fingerprint(session, agent) == challenge.fingerprint
    )


@router.post("/{access_id}/challenge", status_code=201)
def create_challenge(access_id: UUID, response: Response, session: Database):
    initial = access_row(session, access_id, lock=False)
    agent = assigned_agent(session, initial.employee_id, initial.agent_id)
    access = access_row(session, access_id)
    if not authorized_access(session, access, agent):
        raise HTTPException(409, "Approve the identity and verify an enabled channel first")
    channel = channel_row(session, access.channel_id)
    now = datetime.now(UTC)
    for previous in session.scalars(
        select(DeliveryChallenge).where(DeliveryChallenge.access_id == access.id).with_for_update()
    ):
        if previous.accepted_at is None:
            previous.expires_at = now
    token = secrets.token_urlsafe(24)
    challenge = DeliveryChallenge(
        access_id=access.id,
        access_revision=access.revision,
        channel_revision=channel.revision,
        credential_version_id=channel.verified_version_id,
        agent_id=agent.id,
        fingerprint=agent_fingerprint(session, agent),
        application_fingerprint=application_fingerprint(agent.applied_application or {}),
        incarnation_id=agent.current_incarnation_id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=now + timedelta(minutes=15),
    )
    session.add(challenge)
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"id": challenge.id, "token": token, "expires_at": challenge.expires_at}


def claim_challenge(session, inbox, token):
    challenge = session.scalar(
        select(DeliveryChallenge)
        .where(DeliveryChallenge.token_hash == hashlib.sha256(token.encode()).hexdigest())
        .with_for_update()
    )
    now = datetime.now(UTC)
    if not challenge or challenge.consumed_at or challenge.expires_at <= now:
        return None
    access = session.get(EmployeeAccess, challenge.access_id, populate_existing=True)
    if (
        access.channel_id != inbox.channel_id
        or access.external_scope != inbox.external_scope
        or access.external_user_id != inbox.external_user_id
        or not current_challenge(session, challenge, access)
    ):
        return None
    challenge.consumed_at = now
    inbox.challenge_id = challenge.id
    return access


def record_acceptance(session, inbox, outbox):
    """Called only after every response part was accepted by the provider."""
    now = datetime.now(UTC)
    if inbox.challenge_id:
        challenge = session.get(DeliveryChallenge, inbox.challenge_id, with_for_update=True)
        if current_challenge(session, challenge) and challenge.expires_at > now:
            challenge.accepted_at = now
        return
    if not inbox.run_id:
        return
    run = session.get(Run, inbox.run_id)
    if run.status != "completed" or run.source != "employee" or not run.output.strip():
        return
    for challenge in session.scalars(
        select(DeliveryChallenge)
        .where(
            DeliveryChallenge.access_id == inbox.access_id,
            DeliveryChallenge.access_revision == inbox.access_revision,
            DeliveryChallenge.accepted_at.is_not(None),
            DeliveryChallenge.receipt_at.is_(None),
            DeliveryChallenge.accepted_at <= run.created_at,
            DeliveryChallenge.fingerprint == run.availability_fingerprint,
        )
        .with_for_update()
    ):
        if current_challenge(session, challenge):
            challenge.receipt_outbox_id, challenge.receipt_at = outbox.id, now


@router.get("/{access_id}/handoff")
def handoff_state(access_id: UUID, session: Database):
    access = access_row(session, access_id, lock=False)
    history = []
    for challenge in session.scalars(
        select(DeliveryChallenge)
        .where(DeliveryChallenge.access_id == access.id)
        .order_by(DeliveryChallenge.created_at.desc())
        .limit(10)
    ):
        inbox = session.scalar(
            select(ChannelInbox).where(ChannelInbox.challenge_id == challenge.id)
        )
        outbox = (
            session.scalar(select(ChannelOutbox).where(ChannelOutbox.inbox_id == inbox.id))
            if inbox
            else None
        )
        receipt = (
            session.get(ChannelOutbox, challenge.receipt_outbox_id)
            if challenge.receipt_outbox_id
            else None
        )
        receipt_inbox = session.get(ChannelInbox, receipt.inbox_id) if receipt else None
        valid = current_challenge(session, challenge, access)
        history.append(
            {
                "id": challenge.id,
                "created_at": challenge.created_at,
                "expires_at": challenge.expires_at,
                "received_at": challenge.consumed_at,
                "transport_accepted_at": challenge.accepted_at,
                "transport_state": "accepted"
                if challenge.accepted_at
                else outbox.state
                if outbox
                else "expired"
                if challenge.expires_at <= datetime.now(UTC)
                else "pending",
                "current": valid,
                "verified_at": challenge.receipt_at,
                "snapshot": {
                    "access_revision": challenge.access_revision,
                    "channel_revision": challenge.channel_revision,
                    "credential_version_id": challenge.credential_version_id,
                    "agent_id": challenge.agent_id,
                    "incarnation_id": challenge.incarnation_id,
                    "configuration_fingerprint": challenge.fingerprint,
                    "application_fingerprint": challenge.application_fingerprint,
                },
                "receipt": {
                    "run_id": receipt_inbox.run_id,
                    "outbox_id": receipt.id,
                    "provider_ids": receipt.provider_ids,
                    "accepted_parts": receipt.next_part,
                    "total_parts": len(receipt.parts),
                }
                if receipt
                else None,
            }
        )
    return {"history": history}
