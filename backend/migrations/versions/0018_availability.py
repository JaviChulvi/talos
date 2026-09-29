"""Evidence and freshness for service and agent availability."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(32), primary_key=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "availability_checks",
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), primary_key=True),
        sa.Column("kind", sa.String(40), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('ok','blocked','unknown','checking','not_applicable')"),
    )


def downgrade():
    op.drop_table("availability_checks")
    op.drop_table("service_heartbeats")
