"""Explicit probes and admission-time evidence revision."""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("availability_fingerprint", sa.String(64)))
    op.create_table(
        "channel_probes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("employee_channels.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "credential_version_id",
            sa.Uuid(),
            sa.ForeignKey("connection_versions.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("channel_id", "idempotency_key"),
        sa.CheckConstraint("status IN ('queued','running','completed','failed','stale')"),
    )
    op.create_index(
        "one_active_channel_probe",
        "channel_probes",
        ["channel_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade():
    op.drop_table("channel_probes")
    op.drop_column("runs", "availability_fingerprint")
