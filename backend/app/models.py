from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base

ACTIVE_OPERATION_STATUSES = ("queued", "running", "retry_wait")
RUNTIME_RELEASE = "openclaw-2026.9.6"
HERMES_RELEASE = "hermes-0.21.5"
RUNTIME_RELEASES = {"openclaw": RUNTIME_RELEASE, "hermes": HERMES_RELEASE}


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmployeeChannel(Base):
    __tablename__ = "employee_channels"
    __table_args__ = (
        CheckConstraint("provider IN ('telegram','slack')"),
        CheckConstraint("revision > 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("connections.id"), unique=True)
    workspace_id: Mapped[str] = mapped_column(String(40), default="")
    verified_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("connection_versions.id"))
    identity: Mapped[dict] = mapped_column(JSON, default=dict)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmployeeAccess(Base):
    __tablename__ = "employee_accesses"
    __table_args__ = (
        UniqueConstraint("channel_id", "employee_id"),
        UniqueConstraint("channel_id", "external_scope", "external_user_id"),
        CheckConstraint("state IN ('pending','active','disabled')"),
        CheckConstraint("revision > 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("employee_channels.id"), index=True)
    employee_id: Mapped[UUID] = mapped_column(ForeignKey("employees.id"), index=True)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    external_scope: Mapped[str] = mapped_column(String(40), default="")
    external_user_id: Mapped[str | None] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessInvitation(Base):
    __tablename__ = "access_invitations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    access_id: Mapped[UUID] = mapped_column(ForeignKey("employee_accesses.id"), index=True)
    access_revision: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AvailabilityCheck(Base):
    __tablename__ = "availability_checks"
    __table_args__ = (
        CheckConstraint("state IN ('ok','blocked','unknown','checking','not_applicable')"),
    )

    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    state: Mapped[str] = mapped_column(String(20))
    code: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Setup(Base):
    __tablename__ = "setups"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    capture_metadata: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    draft_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    draft_artifact_hash: Mapped[str | None] = mapped_column(String(64))
    revisions: Mapped[list["SetupRevision"]] = relationship(
        lazy="selectin", order_by="SetupRevision.version", passive_deletes="all"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SetupRevision(Base):
    __tablename__ = "setup_revisions"
    __table_args__ = (
        UniqueConstraint("setup_id", "version", name="uq_setup_revision_version"),
        CheckConstraint("version > 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    setup_id: Mapped[UUID] = mapped_column(ForeignKey("setups.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    manifest: Mapped[dict] = mapped_column(JSON)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (CheckConstraint("revision > 0"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    setup_revision_id: Mapped[UUID | None] = mapped_column(ForeignKey("setup_revisions.id"))
    connector_grants: Mapped[list] = mapped_column(JSON, default=list)
    connection_bindings: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class Employee(Base):
    __tablename__ = "employees"
    __table_args__ = (CheckConstraint("monthly_allowance_usd >= 0"),)

    monthly_allowance_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(254))
    connection_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    role_id: Mapped[UUID] = mapped_column(ForeignKey("roles.id"), index=True)
    role: Mapped["Role"] = relationship(lazy="selectin")


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("desired_state IN ('stopped', 'running', 'deleted')"),
        CheckConstraint("revision > 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    runtime_kind: Mapped[str] = mapped_column(String(20), default="openclaw")
    dashboard_password_hash: Mapped[str | None] = mapped_column(Text)
    runtime_mode: Mapped[str] = mapped_column(String(20), default="managed")
    display_name: Mapped[str] = mapped_column(String(120))
    employee_label: Mapped[str] = mapped_column(String(160))
    employee_id: Mapped[UUID | None] = mapped_column(ForeignKey("employees.id"), index=True)
    employee: Mapped["Employee | None"] = relationship(lazy="selectin")

    @property
    def employee_name(self) -> str:
        return self.employee.name if self.employee else self.employee_label

    @property
    def role(self) -> dict | None:
        if self.employee is None:
            return None
        role = self.employee.role
        return {
            "id": role.id,
            "name": role.name,
            "revision": role.revision,
            "capabilities": role.capabilities,
            "setup_revision_id": str(role.setup_revision_id) if role.setup_revision_id else None,
            "connector_grants": role.connector_grants,
        }

    selected_application: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    applied_application: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))

    applied_role: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))

    @property
    def permissions_pending(self) -> bool:
        role = self.role
        return bool(
            role
            and (
                self.applied_role is None
                or self.applied_role["id"] != str(role["id"])
                or self.applied_role["revision"] != role["revision"]
            )
        )

    inference_override: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    runtime_release: Mapped[str] = mapped_column(String(100), default=RUNTIME_RELEASE)
    desired_state: Mapped[str] = mapped_column(String(20), default="stopped")
    observed_state: Mapped[str] = mapped_column(String(20), default="pending")
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    current_incarnation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workload_incarnations.id", name="fk_agents_current_incarnation", use_alter=True)
    )
    current_incarnation: Mapped["WorkloadIncarnation | None"] = relationship(
        foreign_keys=[current_incarnation_id], lazy="selectin"
    )

    @property
    def model_route(self) -> str:
        return self.current_incarnation.model_route if self.current_incarnation else "default"

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
    model_route: Mapped[str] = mapped_column(String(20), default="fixture")
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
        CheckConstraint(
            "action IN ('create', 'start', 'stop', 'delete', 'dashboard', "
            "'apply_role', 'configure_model', 'capture_setup')"
        ),
        CheckConstraint("status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')"),
        CheckConstraint("target_revision > 0"),
        CheckConstraint("attempts >= 0"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    role_application: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    model_selection: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    dashboard_url: Mapped[str | None] = mapped_column(Text)
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


ACTIVE_RUN_STATUSES = ("queued", "dispatching", "running", "cancel_requested", "unknown")
TERMINAL_RUN_STATUSES = ("completed", "cancelled", "failed", "interrupted")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("agent_id", "idempotency_key", name="uq_run_idempotency"),
        Index(
            "uq_run_active_agent",
            "agent_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued', 'dispatching', 'running', 'cancel_requested', 'unknown')"
            ),
        ),
        CheckConstraint(
            "status IN ('queued', 'dispatching', 'running', 'completed', "
            "'cancel_requested', 'cancelled', 'failed', 'unknown', 'interrupted')"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    incarnation_id: Mapped[UUID] = mapped_column(ForeignKey("workload_incarnations.id"))
    model_id: Mapped[str] = mapped_column(String(255), default="fixture")
    inference: Mapped[dict] = mapped_column(JSON, default=dict)
    legacy_inference_calls: Mapped[list] = mapped_column("inference_calls", JSON, default=list)
    calls: Mapped[list["InferenceCall"]] = relationship(
        lazy="selectin", order_by="(InferenceCall.admitted_at, InferenceCall.id)"
    )

    @property
    def inference_calls(self) -> list[dict]:
        return [*(self.legacy_inference_calls or []), *(call.report for call in self.calls)]

    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    upstream_run_id: Mapped[str | None] = mapped_column(String(128))
    output: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (CheckConstraint("sequence > 0"),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InferenceConfig(Base):
    __tablename__ = "inference_config"
    __table_args__ = (CheckConstraint("id = 1"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    model_id: Mapped[str] = mapped_column(String(255), default="fixture")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    tracking_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InferenceCall(Base):
    __tablename__ = "inference_calls"
    __table_args__ = (
        CheckConstraint("cost_usd >= 0"),
        Index("ix_inference_calls_employee_month", "employee_id", "admitted_at"),
        Index("ix_inference_calls_agent_month", "agent_id", "admitted_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id"))
    incarnation_id: Mapped[UUID] = mapped_column(ForeignKey("workload_incarnations.id"))
    employee_id: Mapped[UUID | None] = mapped_column(ForeignKey("employees.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"), index=True)
    model: Mapped[str] = mapped_column(String(255))
    generation_id: Mapped[str | None] = mapped_column(String(200))
    admitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(40), default="unresolved")
    finish_reason: Mapped[str | None] = mapped_column(String(40))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 12))

    @property
    def report(self) -> dict:
        report = {
            "model": self.model,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms or 0,
        }
        for key in (
            "generation_id",
            "finish_reason",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "reasoning_tokens",
        ):
            if (value := getattr(self, key)) is not None:
                report[key] = value
        if self.cost_usd is not None:
            report["cost"] = float(self.cost_usd)
        return report
