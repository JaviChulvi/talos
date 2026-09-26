import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import session_factory
from backend.app.models import ACTIVE_OPERATION_STATUSES, RUNTIME_RELEASE, Agent, Operation

router = APIRouter(prefix="/api/v1")


def get_db() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


Database = Annotated[Session, Depends(get_db)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$")
]
LifecycleAction = Literal["start", "stop", "delete"]


class CreateAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    employee_label: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00]*$")


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    employee_label: str
    runtime_release: str
    desired_state: str
    observed_state: str
    revision: int
    current_incarnation_id: UUID | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class OperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    action: str
    target_revision: int
    status: str
    step: str
    attempts: int
    next_retry_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime


def request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def find_replay(session: Session, scope: str, key: str, digest: str) -> Operation | None:
    operation = session.scalar(
        select(Operation).where(
            Operation.idempotency_scope == scope, Operation.idempotency_key == key
        )
    )
    if operation and operation.request_hash != digest:
        raise HTTPException(409, "Idempotency-Key was already used with a different request")
    return operation


def enqueue_operation(
    session: Session, agent: Agent, action: str, scope: str, key: str, digest: str
) -> Operation:
    """Call within the transaction that creates or locks the agent row."""
    operation = Operation(
        agent_id=agent.id,
        action=action,
        target_revision=agent.revision,
        idempotency_scope=scope,
        idempotency_key=key,
        request_hash=digest,
    )
    session.add(operation)
    session.flush()
    return operation


def recover_duplicate(
    session: Session, scope: str, key: str, digest: str, error: IntegrityError
) -> Operation:
    # The unique constraint serializes concurrent create requests, rolling back
    # both the losing agent and its operation before loading the winner.
    session.rollback()
    constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    if constraint not in {"uq_operation_idempotency", "uq_operation_active_agent"}:
        raise error
    operation = find_replay(session, scope, key, digest)
    if operation:
        return operation
    raise HTTPException(409, "Agent already has an active lifecycle operation")


@router.post("/agents", status_code=202, response_model=OperationResponse)
def create_agent(body: CreateAgent, idempotency_key: IdempotencyKey, session: Database):
    scope = "create-agent"
    digest = request_hash(body.model_dump())
    try:
        with session.begin():
            replay = find_replay(session, scope, idempotency_key, digest)
            if replay:
                return replay
            agent = Agent(**body.model_dump(), runtime_release=RUNTIME_RELEASE)
            session.add(agent)
            session.flush()
            return enqueue_operation(session, agent, "create", scope, idempotency_key, digest)
    except IntegrityError as error:
        return recover_duplicate(session, scope, idempotency_key, digest, error)


@router.get("/agents", response_model=list[AgentResponse])
def list_agents(session: Database):
    return session.scalars(
        select(Agent).where(Agent.observed_state != "deleted").order_by(Agent.created_at, Agent.id)
    ).all()


@router.get("/agents/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: UUID, session: Database):
    agent = session.get(Agent, agent_id)
    if agent is None or agent.observed_state == "deleted":
        raise HTTPException(404, "Agent not found")
    return agent


def request_lifecycle(
    session: Session, agent_id: UUID, action: LifecycleAction, idempotency_key: str
) -> Operation:
    scope = f"agent:{agent_id}:{action}"
    digest = request_hash({"agent_id": str(agent_id), "action": action})
    try:
        with session.begin():
            replay = find_replay(session, scope, idempotency_key, digest)
            if replay:
                return replay
            agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
            # A concurrent request may have committed while this one waited for
            # the row lock. Replay before checking a deletion or active operation.
            replay = find_replay(session, scope, idempotency_key, digest)
            if replay:
                return replay
            if agent is None or agent.observed_state == "deleted":
                raise HTTPException(404, "Agent not found")
            if agent.desired_state == "deleted" and action != "delete":
                raise HTTPException(409, "Agent is being deleted")
            active = session.scalar(
                select(Operation.id).where(
                    Operation.agent_id == agent_id,
                    Operation.status.in_(ACTIVE_OPERATION_STATUSES),
                )
            )
            if active:
                raise HTTPException(409, "Agent already has an active lifecycle operation")
            if (
                action == "start"
                and agent.desired_state == "running"
                and agent.observed_state == "ready"
            ):
                raise HTTPException(409, "Agent is already running; stop it before starting again")
            agent.revision += 1
            agent.desired_state = {"start": "running", "stop": "stopped", "delete": "deleted"}[
                action
            ]
            agent.last_error = None
            return enqueue_operation(session, agent, action, scope, idempotency_key, digest)
    except IntegrityError as error:
        return recover_duplicate(session, scope, idempotency_key, digest, error)


@router.post("/agents/{agent_id}/start", status_code=202, response_model=OperationResponse)
def start_agent(agent_id: UUID, idempotency_key: IdempotencyKey, session: Database):
    return request_lifecycle(session, agent_id, "start", idempotency_key)


@router.post("/agents/{agent_id}/stop", status_code=202, response_model=OperationResponse)
def stop_agent(agent_id: UUID, idempotency_key: IdempotencyKey, session: Database):
    return request_lifecycle(session, agent_id, "stop", idempotency_key)


@router.delete("/agents/{agent_id}", status_code=202, response_model=OperationResponse)
def delete_agent(agent_id: UUID, idempotency_key: IdempotencyKey, session: Database):
    return request_lifecycle(session, agent_id, "delete", idempotency_key)


@router.get("/operations/{operation_id}", response_model=OperationResponse)
def get_operation(operation_id: UUID, session: Database):
    operation = session.get(Operation, operation_id)
    if operation is None:
        raise HTTPException(404, "Operation not found")
    return operation
