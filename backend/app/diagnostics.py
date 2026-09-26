"""Durable diagnostic requests; the worker alone talks to OpenClaw."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.agents import Database, IdempotencyKey, request_hash
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    ACTIVE_RUN_STATUSES,
    Agent,
    Operation,
    Run,
    RunEvent,
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
    status: str
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


@router.post("/agents/{agent_id}/diagnostic-runs", status_code=202, response_model=RunResponse)
def create_run(
    agent_id: UUID, body: DiagnosticRequest, idempotency_key: IdempotencyKey, session: Database
):
    digest = request_hash(body.model_dump())
    with session.begin():
        # Lifecycle admission takes this same lock, so a stop/delete closes
        # admission atomically with its desired-state update.
        agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
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
        run = Run(
            agent_id=agent_id,
            incarnation_id=agent.current_incarnation_id,
            message=body.message,
            idempotency_key=idempotency_key,
            request_hash=digest,
        )
        session.add(run)
        session.flush()
        append_event(session, run, "queued", {})
        return run


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
