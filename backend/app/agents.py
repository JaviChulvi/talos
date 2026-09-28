import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import Database
from backend.app.db import get_db as get_db
from backend.app.models import (
    ACTIVE_OPERATION_STATUSES,
    RUNTIME_RELEASES,
    Agent,
    Employee,
    Operation,
    WorkloadIncarnation,
)

router = APIRouter(prefix="/api/v1")

IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$")
]
LifecycleAction = Literal["start", "stop", "delete", "dashboard", "apply_role"]


class CreateAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    runtime_kind: Literal["openclaw", "hermes"] = "openclaw"
    runtime_mode: Literal["native", "managed"] = "native"
    dashboard_password: SecretStr | None = Field(default=None, min_length=12, max_length=256)

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.runtime_kind == "hermes":
            if self.runtime_mode != "native" or self.dashboard_password is None:
                raise ValueError("Hermes requires native mode and a dashboard password")
        elif self.dashboard_password is not None:
            raise ValueError("OpenClaw uses device pairing, not a dashboard password")
        return self

    display_name: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00]*$")
    employee_label: str = Field(default="", max_length=160, pattern=r"^[^\x00]*$")
    employee_id: UUID | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def employee_identity(self):
        if self.employee_id is None and not self.employee_label:
            raise ValueError("Choose an employee or supply an employee label")
        return self


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    runtime_kind: str
    runtime_mode: str
    display_name: str
    employee_label: str
    employee_id: UUID | None
    employee_name: str
    role: dict | None
    applied_role: dict | None
    selected_application: dict | None
    applied_application: dict | None
    permissions_pending: bool
    setup_status: str = "not_configured"
    setup_pending: bool = False
    setup_blockers: list[str] = Field(default_factory=list)
    inference_override: dict | None
    runtime_release: str
    model_route: str
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
    dashboard_url: str | None
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
async def create_agent(body: CreateAgent, idempotency_key: IdempotencyKey, session: Database):
    scope = "create-agent"
    payload = body.model_dump(exclude={"dashboard_password", "model_id"})
    request = {key: value for key, value in payload.items() if key != "runtime_kind"}
    if body.employee_id is not None:
        request["employee_id"] = str(body.employee_id)
    if body.employee_id is None:
        request.pop("employee_id")  # Retain replay hashes for legacy requests.
    if body.model_id is not None:
        request["model_id"] = body.model_id
    if body.runtime_kind == "hermes":
        request.update(
            runtime_kind="hermes",
            # Keep replay comparison expensive to guess, like the stored login hash.
            dashboard_password=hashlib.scrypt(
                body.dashboard_password.get_secret_value().encode(),
                salt=hashlib.sha256(idempotency_key.encode()).digest(),
                n=16384,
                r=8,
                p=1,
                dklen=32,
            ).hex(),
        )
    digest = request_hash(request)
    # Replay before catalog/network validation so a provider outage cannot hide a success.
    replay = find_replay(session, scope, idempotency_key, digest)
    session.rollback()
    if replay:
        return replay
    selection = None
    if body.model_id is not None:
        from backend.app.inference import ModelSelection, validated_selection

        selection = await validated_selection(ModelSelection(model_id=body.model_id))
        if body.runtime_mode == "native" and body.model_id == "fixture":
            raise HTTPException(400, "Native agents require an OpenRouter model")
    try:
        with session.begin():
            replay = find_replay(session, scope, idempotency_key, digest)
            if replay:
                return replay
            if (
                body.employee_id
                and session.get(Employee, body.employee_id, with_for_update=True) is None
            ):
                raise HTTPException(404, "Employee not found")
            password_hash = None
            if body.dashboard_password:
                # Format verified against Hermes's bundled password provider.
                salt = secrets.token_bytes(16)
                key = hashlib.scrypt(
                    body.dashboard_password.get_secret_value().encode(),
                    salt=salt,
                    n=16384,
                    r=8,
                    p=1,
                    dklen=32,
                )
                password_hash = (
                    "scrypt$16384$8$1$"
                    + base64.b64encode(salt).decode()
                    + "$"
                    + base64.b64encode(key).decode()
                )
            agent = Agent(
                **payload,
                runtime_release=RUNTIME_RELEASES[body.runtime_kind],
                dashboard_password_hash=password_hash,
                inference_override=selection,
            )
            session.add(agent)
            session.flush()
            return enqueue_operation(session, agent, "create", scope, idempotency_key, digest)
    except IntegrityError as error:
        return recover_duplicate(session, scope, idempotency_key, digest, error)


@router.get("/agents", response_model=list[AgentResponse])
def list_agents(session: Database):
    from backend.app.applications import annotate_agent

    agents = session.scalars(
        select(Agent).where(Agent.observed_state != "deleted").order_by(Agent.created_at, Agent.id)
    ).all()
    return [annotate_agent(session, agent) for agent in agents]


@router.get("/agents/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: UUID, session: Database):
    agent = session.get(Agent, agent_id)
    if agent is None or agent.observed_state == "deleted":
        raise HTTPException(404, "Agent not found")
    from backend.app.applications import annotate_agent

    return annotate_agent(session, agent)


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
            if action == "dashboard":
                if (
                    agent.runtime_mode != "native"
                    or agent.desired_state != "running"
                    or agent.observed_state != "ready"
                ):
                    raise HTTPException(409, "Start a native agent first")
                return enqueue_operation(session, agent, action, scope, idempotency_key, digest)
            application = None
            if action == "apply_role" and (
                agent.employee_id is None or agent.runtime_mode != "native"
            ):
                raise HTTPException(409, "Choose an employee for a native agent first")
            if (
                action in {"start", "apply_role"}
                and agent.employee_id
                and agent.runtime_mode == "native"
            ):
                from backend.app.applications import desired_application, normalize_application

                if action == "start" and agent.selected_application:
                    application = normalize_application(
                        agent.selected_application, agent.runtime_kind
                    )
                    if application.get("employee_id") != str(agent.employee_id):
                        raise HTTPException(409, "Employee changed; apply the role before starting")
                else:
                    application = desired_application(session, agent)
                    agent.selected_application = application
                application = {
                    **application,
                    "restart": action == "start" or agent.desired_state == "running",
                }
            agent.revision += 1
            agent.desired_state = {
                "start": "running",
                "stop": "stopped",
                "delete": "deleted",
                "apply_role": agent.desired_state,
            }[action]
            if action in {"stop", "delete"} and agent.current_incarnation_id:
                incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
                incarnation.revoked_at = datetime.now(UTC)
            agent.last_error = None
            operation = enqueue_operation(session, agent, action, scope, idempotency_key, digest)
            operation.role_application = application
            return operation
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
def get_operation(operation_id: UUID, session: Database, response: Response):
    response.headers["Cache-Control"] = "no-store"
    operation = session.get(Operation, operation_id)
    if operation is None:
        raise HTTPException(404, "Operation not found")
    return operation


@router.post("/agents/{agent_id}/dashboard", status_code=202, response_model=OperationResponse)
def open_dashboard(
    agent_id: UUID, idempotency_key: IdempotencyKey, session: Database, response: Response
):
    response.headers["Cache-Control"] = "no-store"
    return request_lifecycle(session, agent_id, "dashboard", idempotency_key)


class AssignEmployee(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: UUID


@router.put("/agents/{agent_id}/employee", response_model=AgentResponse)
def assign_employee(agent_id: UUID, body: AssignEmployee, session: Database):
    with session.begin():
        agent = session.get(Agent, agent_id, with_for_update=True)
        if agent is None or agent.desired_state == "deleted":
            raise HTTPException(404, "Agent not found")
        if (
            agent.desired_state != "stopped"
            or agent.observed_state != "stopped"
            or session.scalar(
                select(Operation.id).where(
                    Operation.agent_id == agent_id, Operation.status.in_(ACTIVE_OPERATION_STATUSES)
                )
            )
        ):
            raise HTTPException(409, "Stop the agent and wait for its operation before assigning")
        employee = session.get(Employee, body.employee_id, with_for_update=True)
        if employee is None:
            raise HTTPException(404, "Employee not found")
        agent.employee = employee
    return agent


@router.post("/agents/{agent_id}/apply-role", status_code=202, response_model=OperationResponse)
def apply_role(agent_id: UUID, idempotency_key: IdempotencyKey, session: Database):
    return request_lifecycle(session, agent_id, "apply_role", idempotency_key)


@router.post("/agents/{agent_id}/setup-preview")
def setup_preview(agent_id: UUID, session: Database):
    from backend.app.applications import application_preview

    agent = session.get(Agent, agent_id)
    if agent is None or agent.desired_state == "deleted":
        raise HTTPException(404, "Agent not found")
    return application_preview(session, agent)
