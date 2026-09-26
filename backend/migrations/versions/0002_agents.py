"""Persist agents, workload identities, and idempotent lifecycle operations."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def timestamps():
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade():
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("employee_label", sa.String(160), nullable=False),
        sa.Column("runtime_release", sa.String(100), nullable=False),
        sa.Column("desired_state", sa.String(20), nullable=False),
        sa.Column("observed_state", sa.String(20), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("current_incarnation_id", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("desired_state IN ('stopped', 'running', 'deleted')"),
        sa.CheckConstraint("revision > 0"),
    )
    op.create_table(
        "workload_incarnations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("runtime_release", sa.String(100), nullable=False),
        sa.Column("image_digest", sa.String(255), nullable=True),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("container_id", sa.String(64), nullable=True, unique=True),
        sa.Column("container_name", sa.String(128), nullable=True, unique=True),
        sa.Column("config_volume", sa.String(128), nullable=True, unique=True),
        sa.Column("gateway_token_hash", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.UniqueConstraint("agent_id", "generation", name="uq_incarnation_generation"),
        sa.CheckConstraint("generation > 0"),
    )
    op.create_index("ix_workload_incarnations_agent_id", "workload_incarnations", ["agent_id"])
    op.create_foreign_key(
        "fk_agents_current_incarnation",
        "agents",
        "workload_incarnations",
        ["current_incarnation_id"],
        ["id"],
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("target_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("step", sa.String(80), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.String(160), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_scope", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "idempotency_scope", "idempotency_key", name="uq_operation_idempotency"
        ),
        sa.CheckConstraint("action IN ('create', 'start', 'stop', 'delete')"),
        sa.CheckConstraint("status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')"),
        sa.CheckConstraint("target_revision > 0"),
        sa.CheckConstraint("attempts >= 0"),
    )
    op.create_index("ix_operations_agent_id", "operations", ["agent_id"])
    op.create_index(
        "ix_operations_pending", "operations", ["status", "next_retry_at", "created_at"]
    )
    op.create_index(
        "uq_operation_active_agent",
        "operations",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'retry_wait')"),
    )


def downgrade():
    op.drop_table("operations")
    op.drop_constraint("fk_agents_current_incarnation", "agents", type_="foreignkey")
    op.drop_table("workload_incarnations")
    op.drop_table("agents")
