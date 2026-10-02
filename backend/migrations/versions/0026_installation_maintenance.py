"""Durable maintenance admission and restored channel ingress boundaries."""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "installation_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release", sa.String(100), nullable=True),
        sa.Column("maintenance_operation_id", sa.String(128), nullable=True),
        sa.Column("maintenance_kind", sa.String(20), nullable=True),
        sa.Column("maintenance_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1"),
    )
    op.execute("INSERT INTO installation_state (id) VALUES (1)")
    op.add_column(
        "channel_cursors",
        sa.Column(
            "reconnect_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "channel_cursors",
        sa.Column(
            "accept_after",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("channel_cursors", "accept_after")
    op.drop_column("channel_cursors", "reconnect_required")
    op.drop_table("installation_state")
