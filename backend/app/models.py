from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base

ACTIVE_OPERATION_STATUSES = ("queued", "running", "retry_wait")
RUNTIME_RELEASE = "openclaw-2026.9.6"


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("desired_state IN ('stopped', 'running', 'deleted')"),
        CheckConstraint("revision > 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(120))
    employee_label: Mapped[str] = mapped_column(String(160))
    runtime_release: Mapped[str] = mapped_column(String(100), default=RUNTIME_RELEASE)
    desired_state: Mapped[str] = mapped_column(String(20), default="stopped")
    observed_state: Mapped[str] = mapped_column(String(20), default="pending")
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    current_incarnation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workload_incarnations.id", name="fk_agents_current_incarnation", use_alter=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkloadIncarnation(Base):
    __tablename__ = "workload_incarnations"
    __table_args__ = (
        UniqueConstraint("agent_id", "generation", name="uq_incarnation_generation"),
        CheckConstraint("generation > 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    runtime_release: Mapped[str] = mapped_column(String(100), default=RUNTIME_RELEASE)
    image_digest: Mapped[str | None] = mapped_column(String(255))
    config_hash: Mapped[str | None] = mapped_column(String(64))
    container_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    container_name: Mapped[str | None] = mapped_column(String(128), unique=True)
    config_volume: Mapped[str | None] = mapped_column(String(128), unique=True)
    gateway_token_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_operation_idempotency"),
        Index(
            "uq_operation_active_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'retry_wait')"),
        ),
        Index("ix_operations_pending", "status", "next_retry_at", "created_at"),
        CheckConstraint("action IN ('create', 'start', 'stop', 'delete')"),
        CheckConstraint("status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')"),
        CheckConstraint("target_revision > 0"),
        CheckConstraint("attempts >= 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    action: Mapped[str] = mapped_column(String(20))
    target_revision: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    step: Mapped[str] = mapped_column(String(80), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner: Mapped[str | None] = mapped_column(String(160))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    idempotency_scope: Mapped[str] = mapped_column(String(160))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
