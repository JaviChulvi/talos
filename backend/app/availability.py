"""Read availability from durable evidence, never probe from a GET request."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.app.db import Database
from backend.app.models import Agent, AvailabilityCheck, InferenceConfig, ServiceHeartbeat

router = APIRouter(prefix="/api/v1", tags=["availability"])
HEARTBEAT_TTL = timedelta(seconds=30)
CHECK_TTL = timedelta(seconds=60)
CHECKS = ("runtime", "model", "setup", "connections")
ACTIONS = {
    "worker": "Check the worker service",
    "gateway": "Check the gateway service",
    "runtime": "Start the agent or inspect its runtime",
    "model": "Configure or test the selected model",
    "setup": "Check or apply the selected setup",
    "connections": "Check the required connections",
}


def heartbeat(session: Session, service: str, now: datetime | None = None):
    stamp = now or datetime.now(UTC)
    session.execute(
        insert(ServiceHeartbeat)
        .values(service=service, checked_at=stamp)
        .on_conflict_do_update(index_elements=["service"], set_={"checked_at": stamp})
    )


def platform_status(session: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    records = {
        row.service: row.checked_at
        for row in session.scalars(
            select(ServiceHeartbeat).execution_options(populate_existing=True)
        )
    }
    return {
        service: {
            "state": "unknown"
            if service not in records
            else "ok"
            if now < records[service] + HEARTBEAT_TTL
            else "stale",
            "checked_at": records.get(service),
            "expires_at": records[service] + HEARTBEAT_TTL if service in records else None,
            "action": ACTIONS[service],
        }
        for service in ("worker", "gateway")
    }


def agent_fingerprint(session: Session, agent: Agent) -> str:
    config = session.get(InferenceConfig, 1) if agent.runtime_mode != "native" else None
    payload = {
        "revision": agent.revision,
        "incarnation": str(agent.current_incarnation_id),
        "employee": str(agent.employee_id),
        "runtime": agent.runtime_release,
        "application": agent.applied_application,
        "selected": agent.selected_application,
        "model": agent.inference_override,
        "default_model": {"model": config.model_id, "settings": config.settings}
        if config and not agent.inference_override
        else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def record_check(
    session: Session,
    agent: Agent,
    kind: str,
    state: str,
    code: str,
    *,
    fingerprint: str | None = None,
    now: datetime | None = None,
    ttl: timedelta = CHECK_TTL,
):
    now = now or datetime.now(UTC)
    values = {
        "agent_id": agent.id,
        "kind": kind,
        "state": state,
        "code": code,
        "fingerprint": fingerprint or agent_fingerprint(session, agent),
        "checked_at": now,
        "expires_at": now + ttl,
    }
    session.execute(
        insert(AvailabilityCheck)
        .values(**values)
        .on_conflict_do_update(index_elements=["agent_id", "kind"], set_=values)
    )


def availability(session: Session, agent: Agent, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    fingerprint = agent_fingerprint(session, agent)
    records = {
        row.kind: row
        for row in session.scalars(
            select(AvailabilityCheck)
            .where(AvailabilityCheck.agent_id == agent.id)
            .execution_options(populate_existing=True)
        )
    }
    services = platform_status(session, now)
    checks = [{"kind": "worker", **services["worker"]}]
    if agent.runtime_mode != "native" or agent.inference_override:
        checks.append({"kind": "gateway", **services["gateway"]})
    for kind in CHECKS:
        row = records.get(kind)
        application = agent.applied_application or {}
        not_required = (kind == "setup" and not application.get("setup")) or (
            kind == "connections" and not application.get("connector_grants")
        )
        incarnation = agent.current_incarnation
        identity_expired = (
            kind == "runtime"
            and incarnation is not None
            and (
                incarnation.revoked_at is not None
                or (incarnation.expires_at is not None and incarnation.expires_at <= now)
            )
        )
        checks.append(
            {
                "kind": kind,
                "state": "not_applicable"
                if not_required
                else "blocked"
                if identity_expired
                else "unknown"
                if row is None
                else "stale"
                if row.fingerprint != fingerprint or row.expires_at <= now
                else row.state,
                "code": "runtime_identity_inactive"
                if identity_expired
                else row.code
                if row
                else "not_verified",
                "checked_at": row.checked_at if row else None,
                "expires_at": row.expires_at if row else None,
                "action": ACTIONS[kind],
            }
        )
    if (
        agent.runtime_mode != "native" and agent.model_route != "fixture"
    ) or agent.inference_override:
        from backend.app.models import Employee
        from backend.app.usage import employee_budget

        employee = session.get(Employee, agent.employee_id) if agent.employee_id else None
        budget = employee_budget(session, employee, now) if employee else None
        checks.append(
            {
                "kind": "allowance",
                "state": "blocked" if not employee or budget["status"] == "exhausted" else "ok",
                "code": "employee_assignment_required"
                if not employee
                else "allowance_exhausted"
                if budget["status"] == "exhausted"
                else "allowance_available",
                "action": "Assign an employee or review their monthly allowance",
                "checked_at": now,
                "expires_at": None,
            }
        )
    running = agent.desired_state == "running" and agent.observed_state == "ready"
    states = {check["state"] for check in checks}
    status = (
        "stopped"
        if agent.desired_state != "running"
        else "requires_action"
        if not running or "blocked" in states
        else "checking"
        if "checking" in states
        else "unverified"
        if states & {"unknown", "stale"}
        else "available"
    )
    from backend.app.applications import annotate_agent
    from backend.app.models import EmployeeAccess, EmployeeChannel
    from backend.app.readiness import channel_status

    accesses = []
    for access in session.scalars(
        select(EmployeeAccess).where(EmployeeAccess.agent_id == agent.id)
    ):
        channel = session.get(EmployeeChannel, access.channel_id)
        check = channel_status(session, channel, now)
        accesses.append(
            {
                "access_id": access.id,
                "channel_id": channel.id,
                "provider": channel.provider,
                "identity_state": access.state,
                "channel": check,
                "status": "available"
                if status == "available" and access.state == "active" and check["state"] == "ok"
                else "requires_action"
                if access.state != "active" or check["state"] in ("blocked", "not_applicable")
                else "unverified",
            }
        )

    annotate_agent(session, agent)
    return {
        "agent_id": agent.id,
        "status": status,
        "fingerprint": fingerprint,
        "checks": checks,
        "accesses": accesses,
        "update_available": agent.setup_pending,
        "pending_blockers": agent.setup_blockers,
    }


@router.get("/agents/{agent_id}/availability")
def get_availability(agent_id: UUID, session: Database):
    agent = session.get(Agent, agent_id)
    if agent is None or agent.observed_state == "deleted":
        raise HTTPException(404, "Agent not found")
    return availability(session, agent)
