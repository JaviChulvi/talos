"""Scoped transport challenges and persisted first-reply delivery receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "delivery_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("access_id", sa.Uuid(), sa.ForeignKey("employee_accesses.id"), nullable=False),
        sa.Column("access_revision", sa.Integer(), nullable=False),
        sa.Column("channel_revision", sa.Integer(), nullable=False),
        sa.Column(
            "credential_version_id",
            sa.Uuid(),
            sa.ForeignKey("connection_versions.id"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("application_fingerprint", sa.String(64), nullable=False),
        sa.Column("incarnation_id", sa.Uuid(), sa.ForeignKey("workload_incarnations.id")),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("receipt_outbox_id", sa.Uuid(), sa.ForeignKey("channel_outbox.id")),
        sa.Column("receipt_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_delivery_challenges_access_id", "delivery_challenges", ["access_id"])
    op.add_column(
        "channel_inbox",
        sa.Column("challenge_id", sa.Uuid(), sa.ForeignKey("delivery_challenges.id")),
    )


def downgrade():
    op.drop_column("channel_inbox", "challenge_id")
    op.drop_table("delivery_challenges")
