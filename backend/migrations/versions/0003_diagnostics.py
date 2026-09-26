"""Persist diagnostic delivery intent and ordered result events."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column(
            "incarnation_id", sa.Uuid(), sa.ForeignKey("workload_incarnations.id"), nullable=False
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("upstream_run_id", sa.String(128), nullable=True),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("error", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("agent_id", "idempotency_key", name="uq_run_idempotency"),
        sa.CheckConstraint(
            "status IN ('queued', 'dispatching', 'running', 'completed', "
            "'cancel_requested', 'cancelled', 'failed', 'unknown', 'interrupted')"
        ),
    )
    op.create_index("ix_runs_agent_id", "runs", ["agent_id"])
    op.create_index(
        "uq_run_active_agent",
        "runs",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'dispatching', 'running', 'cancel_requested', 'unknown')"
        ),
    )
    op.create_table(
        "run_events",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("sequence > 0"),
    )


def downgrade():
    op.drop_table("run_events")
    op.drop_table("runs")
