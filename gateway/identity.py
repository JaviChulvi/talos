import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app.db import session_factory
from backend.app.models import Agent, WorkloadIncarnation


def validate_token(token: str) -> bool:
    if not 20 <= len(token) <= 256:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    try:
        with session_factory()() as session:
            incarnation = session.scalar(
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
            return incarnation is not None
    except SQLAlchemyError:
        return False
