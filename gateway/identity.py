import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import session_factory
from backend.app.models import Agent, InferenceCall, Run, WorkloadIncarnation


def validate_token(token: str, *, require_run: bool = False, run_id=None) -> bool:
    if not 20 <= len(token) <= 256:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        with session_factory()() as session:
            query = (
                select(WorkloadIncarnation.id)
                .join(Agent, Agent.current_incarnation_id == WorkloadIncarnation.id)
                .where(
                    Agent.id == WorkloadIncarnation.agent_id,
                    Agent.desired_state == "running",
                    Agent.observed_state == "ready",
                    WorkloadIncarnation.gateway_token_hash == digest,
                    WorkloadIncarnation.revoked_at.is_(None),
                    WorkloadIncarnation.expires_at > datetime.now(UTC),
                )
            )
            if require_run:
                query = query.where(
                    select(Run.id)
                    .where(
                        Run.incarnation_id == WorkloadIncarnation.id,
                        Run.status.in_(("dispatching", "running")),
                        Run.cancel_requested.is_(False),
                        Run.id == run_id if run_id is not None else True,
                    )
                    .exists()
                )
            return session.scalar(query) is not None
    except SQLAlchemyError:
        return False


def selected_request(token: str) -> dict:
    digest = hashlib.sha256(token.encode()).hexdigest()
    with session_factory()() as session:
        run = session.scalar(
            select(Run)
            .join(WorkloadIncarnation, Run.incarnation_id == WorkloadIncarnation.id)
            .where(
                WorkloadIncarnation.gateway_token_hash == digest,
                Run.status.in_(("dispatching", "running")),
                Run.cancel_requested.is_(False),
            )
        )
        if run is None:
            raise ValueError("No admitted inference request")
        return {"run_id": str(run.id), "model_id": run.model_id, **run.inference}


def native_selection(token: str) -> dict | None:
    """Native entry points use their incarnation identity without a Talos chat run."""
    if not validate_token(token):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with session_factory()() as session:
        return session.scalar(
            select(Agent.inference_override)
            .join(WorkloadIncarnation, Agent.current_incarnation_id == WorkloadIncarnation.id)
            .where(WorkloadIncarnation.gateway_token_hash == digest, Agent.runtime_mode == "native")
        )


def usage_values(usage: dict) -> dict:
    """Accept reported values only. Unknown and invalid accounting is never zero."""
    result = {}
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(source)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result[target] = value
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result["reasoning_tokens"] = value
    value = usage.get("cost")
    if type(value) in (int, float, Decimal):
        try:
            cost = Decimal(str(value))
            if cost.is_finite() and 0 <= cost < Decimal("1000000000000"):
                result["cost"] = cost.quantize(Decimal("0.000000000001"))
        except InvalidOperation:
            pass
    return result


def admit_inference(token: str, run_id: str | None = None) -> UUID:
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        with session_factory().begin() as session:
            agent = session.scalar(
                select(Agent)
                .join(WorkloadIncarnation, Agent.current_incarnation_id == WorkloadIncarnation.id)
                .where(WorkloadIncarnation.gateway_token_hash == digest)
                .with_for_update(of=Agent)
            )
            if agent is None:
                raise HTTPException(401, "Invalid or inactive agent identity")
            incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
            if (
                agent.desired_state != "running"
                or agent.observed_state != "ready"
                or incarnation.agent_id != agent.id
                or incarnation.revoked_at is not None
                or incarnation.expires_at <= datetime.now(UTC)
            ):
                raise HTTPException(401, "Invalid or inactive agent identity")
            if run_id is not None:
                run = session.get(Run, UUID(run_id), with_for_update=True)
                if (
                    run is None
                    or run.incarnation_id != incarnation.id
                    or run.status not in ("dispatching", "running")
                    or run.cancel_requested
                ):
                    raise HTTPException(401, "No admitted inference request")
                model = run.model_id
            else:
                if agent.runtime_mode != "native" or not agent.inference_override:
                    raise HTTPException(401, "Native OpenRouter access is inactive")
                model = agent.inference_override["model_id"]
            call = InferenceCall(
                agent_id=agent.id,
                incarnation_id=incarnation.id,
                employee_id=agent.employee_id,
                run_id=UUID(run_id) if run_id else None,
                model=model,
            )
            session.add(call)
            session.flush()
            return call.id
    except SQLAlchemyError:
        raise HTTPException(503, "Inference accounting is unavailable") from None


def record_inference(call_id: UUID, report: dict):
    with session_factory().begin() as session:
        call = session.get(InferenceCall, call_id, with_for_update=True)
        if call.completed_at is not None:
            return
        values = usage_values(
            {
                "prompt_tokens": report.get("input_tokens"),
                "completion_tokens": report.get("output_tokens"),
                "total_tokens": report.get("total_tokens"),
                "completion_tokens_details": {"reasoning_tokens": report.get("reasoning_tokens")},
                "cost": report.get("cost"),
            }
        )
        for key, value in values.items():
            setattr(call, "cost_usd" if key == "cost" else key, value)
        for key, limit in (("generation_id", 200), ("finish_reason", 40), ("outcome", 40)):
            if isinstance(report.get(key), str):
                setattr(call, key, report[key][:limit])
        duration = report.get("duration_ms")
        if type(duration) is int and 0 <= duration <= 2**63 - 1:
            call.duration_ms = duration
        call.completed_at = datetime.now(UTC)
