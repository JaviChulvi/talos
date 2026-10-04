"""Durable conversation requests; only the worker talks to agent runtimes."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.agents import Database, IdempotencyKey, request_hash
from backend.app.availability import agent_fingerprint
from backend.app.channels import authorized_access
from backend.app.inference import config_response
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    ACTIVE_RUN_STATUSES,
    Agent,
    InferenceConfig,
    Operation,
    Run,
    RunEvent,
    UserAccess,
    UserChannel,
)

router = APIRouter(prefix="/api/v1")


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(min_length=1, max_length=4000, pattern=r"^[^\x00]*$")


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    incarnation_id: UUID
    message: str
    source: str
    access_id: UUID | None
    user_id: UUID | None
    status: str
    model_id: str
    inference: dict
    inference_calls: list[dict]
    cancel_requested: bool
    output: str
    error: str | None
    created_at: datetime
    updated_at: datetime


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    type: str
    payload: dict
    created_at: datetime


def append_event(session: Session, run: Run, kind: str, payload: dict) -> None:
    """Caller must lock the run (or own its new, uncommitted row)."""
    run.event_count += 1
    session.add(RunEvent(run_id=run.id, sequence=run.event_count, type=kind, payload=payload))


def replay_run(session: Session, agent_id: UUID, key: str, digest: str) -> Run | None:
    run = session.scalar(select(Run).where(Run.agent_id == agent_id, Run.idempotency_key == key))
    if run and run.request_hash != digest:
        raise HTTPException(409, "Idempotency-Key was already used with a different request")
    return run


def admit_run(
    session: Session,
    agent_id: UUID,
    body: DiagnosticRequest,
    idempotency_key: str,
    *,
    access_id: UUID | None = None,
    expected_channel_revision: int | None = None,
) -> Run:
    """The caller owns the transaction, so channel receipt and admission can commit together."""
    # Lifecycle admission takes this same lock, so a stop/delete closes
    # admission atomically with its desired-state update.
    agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
    access = None
    channel = None
    if access_id is not None:
        access = session.get(UserAccess, access_id, with_for_update=True, populate_existing=True)
        if agent is None or not authorized_access(
            session, access, agent, channel_revision=expected_channel_revision
        ):
            raise HTTPException(403, "User access is inactive or changed")
        channel = session.get(UserChannel, access.channel_id)
    digest = request_hash(
        body.model_dump()
        if access is None
        else {
            "message": body.message,
            "access_id": str(access.id),
            "revision": access.revision,
            "channel_revision": channel.revision,
        }
    )
    replay = replay_run(session, agent_id, idempotency_key, digest)
    if replay:
        return replay
    if agent is None or agent.observed_state == "deleted":
        raise HTTPException(404, "Agent not found")
    if (
        agent.desired_state != "running"
        or agent.observed_state != "ready"
        or agent.current_incarnation_id is None
    ):
        raise HTTPException(409, "Agent must be running and ready")
    if session.scalar(
        select(Operation.id).where(
            Operation.agent_id == agent_id, Operation.status.in_(ACTIVE_OPERATION_STATUSES)
        )
    ):
        raise HTTPException(409, "Agent has an active lifecycle operation")
    if session.scalar(
        select(Run.id).where(Run.agent_id == agent_id, Run.status.in_(ACTIVE_RUN_STATUSES))
    ):
        raise HTTPException(409, "Agent has an active or unresolved diagnostic run")
    native = agent.runtime_mode == "native"
    config = agent.inference_override or config_response(session.get(InferenceConfig, 1))
    managed = not native and agent.model_route != "fixture"
    if managed and config["model_id"] != "fixture" and not config["capabilities"]:
        raise HTTPException(409, "Apply the model selection once to load its capabilities")
    run = Run(
        agent_id=agent_id,
        incarnation_id=agent.current_incarnation_id,
        message=body.message,
        source="user" if access else "admin",
        availability_fingerprint=agent_fingerprint(session, agent),
        user_id=agent.user_id,
        access_id=access.id if access else None,
        access_revision=access.revision if access else None,
        channel_revision=channel.revision if channel else None,
        # This opaque namespace is persisted in native runtime chat histories.
        session_key=f"agent:main:employee:{channel.provider}:{access.id}:{access.revision}:{agent.id}"
        if access
        else None,
        model_id="native" if native else config["model_id"] if managed else "fixture",
        inference={
            "settings": config["settings"],
            "capabilities": config["capabilities"],
            "source": "agent" if agent.inference_override else "workspace",
        }
        if managed
        else {"source": "native"}
        if native
        else {},
        idempotency_key=idempotency_key,
        request_hash=digest,
    )
    session.add(run)
    session.flush()
    append_event(session, run, "queued", {})
    return run


def admit_user_run(
    session: Session,
    access_id: UUID,
    message: str,
    idempotency_key: str,
    *,
    channel_revision: int,
) -> Run:
    access = session.get(UserAccess, access_id)
    if access is None:
        raise HTTPException(403, "User access is inactive or changed")
    return admit_run(
        session,
        access.agent_id,
        DiagnosticRequest(message=message),
        idempotency_key,
        access_id=access_id,
        expected_channel_revision=channel_revision,
    )


def authorized_run(session: Session, run: Run, agent: Agent) -> bool:
    if run.source != "user":
        return True
    if run.user_id != agent.user_id:
        return False
    access = session.get(UserAccess, run.access_id, populate_existing=True)
    return authorized_access(
        session,
        access,
        agent,
        access_revision=run.access_revision,
        channel_revision=run.channel_revision,
    )


@router.post("/agents/{agent_id}/diagnostic-runs", status_code=202, response_model=RunResponse)
def create_run(
    agent_id: UUID, body: DiagnosticRequest, idempotency_key: IdempotencyKey, session: Database
):
    with session.begin():
        return admit_run(session, agent_id, body, idempotency_key)


@router.get("/agents/{agent_id}/runs", response_model=list[RunResponse])
def list_runs(
    agent_id: UUID,
    session: Database,
    source: Literal["admin", "user", "probe", "all"] = "admin",
):
    if session.get(Agent, agent_id) is None:
        raise HTTPException(404, "Agent not found")
    return session.scalars(
        select(Run)
        .where(Run.agent_id == agent_id, Run.source == source if source != "all" else True)
        .order_by(Run.created_at.desc())
        .limit(20)
    ).all()


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: UUID, session: Database):
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Diagnostic run not found")
    return run


@router.get("/runs/{run_id}/events", response_model=list[EventResponse])
def get_events(run_id: UUID, session: Database, after: Annotated[int, Query(ge=0)] = 0):
    if session.get(Run, run_id) is None:
        raise HTTPException(404, "Diagnostic run not found")
    return session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
        .order_by(RunEvent.sequence)
        .limit(100)
    ).all()


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(run_id: UUID, session: Database):
    with session.begin():
        run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
        if run is None:
            raise HTTPException(404, "Diagnostic run not found")
        if run.status not in ACTIVE_RUN_STATUSES or run.cancel_requested:
            return run
        run.cancel_requested = True
        if run.status == "queued":
            run.status = "cancelled"
        elif run.status != "unknown":
            run.status = "cancel_requested"
        append_event(session, run, run.status, {"cancel_requested": True})
        return run


def mark_runs_stopped(session: Session, agent_id: UUID) -> None:
    """Call only after Docker confirms stop/removal, while holding the agent lock."""
    runs = session.scalars(
        select(Run)
        .where(Run.agent_id == agent_id, Run.status.in_(ACTIVE_RUN_STATUSES))
        .with_for_update()
    ).all()
    for run in runs:
        run.status = "interrupted"
        run.error = "Runtime stopped; diagnostic will not be retried"
        append_event(session, run, "interrupted", {"reason": "runtime_stopped"})
