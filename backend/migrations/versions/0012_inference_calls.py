"""Durable provider call accounting; historical run JSON is retained unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "inference_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column(
            "incarnation_id", sa.Uuid(), sa.ForeignKey("workload_incarnations.id"), nullable=False
        ),
        sa.Column("employee_id", sa.Uuid(), sa.ForeignKey("employees.id")),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id")),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("generation_id", sa.String(200)),
        sa.Column(
            "admitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("finish_reason", sa.String(40)),
        sa.Column("duration_ms", sa.BigInteger()),
        *[
            sa.Column(name, sa.BigInteger())
            for name in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")
        ],
        sa.Column("cost_usd", sa.Numeric(24, 12)),
        sa.CheckConstraint("cost_usd >= 0"),
    )
    op.create_index("ix_inference_calls_run_id", "inference_calls", ["run_id"])
    op.create_index(
        "ix_inference_calls_employee_month", "inference_calls", ["employee_id", "admitted_at"]
    )
    op.create_index(
        "ix_inference_calls_agent_month", "inference_calls", ["agent_id", "admitted_at"]
    )
    op.add_column(
        "inference_config",
        sa.Column(
            "tracking_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_column("inference_config", "tracking_started_at")
    op.drop_table("inference_calls")
