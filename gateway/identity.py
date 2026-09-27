import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import session_factory
from backend.app.models import Agent, Run, WorkloadIncarnation


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


def record_inference(run_id: str, report: dict):
    from uuid import UUID

    with session_factory().begin() as session:
        run = session.get(Run, UUID(run_id), with_for_update=True)
        run.inference_calls = [*run.inference_calls, report]
