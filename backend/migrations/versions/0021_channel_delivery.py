"""Durable transport cursor, sanitized inbox and response send intent."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "channel_cursors",
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("employee_channels.id"), primary_key=True),
        sa.Column("credential_version_id", sa.Uuid(), sa.ForeignKey("connection_versions.id")),
        sa.Column("provider_identity", sa.String(80), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "channel_inbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("employee_channels.id"), nullable=False),
        sa.Column("channel_revision", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(160), nullable=False),
        sa.Column("external_user_id", sa.String(40), nullable=False),
        sa.Column("external_scope", sa.String(40), nullable=False),
        sa.Column("destination", sa.String(80), nullable=False),
        sa.Column("access_id", sa.Uuid(), sa.ForeignKey("employee_accesses.id")),
        sa.Column("access_revision", sa.Integer()),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id")),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("channel_id", "event_id"),
    )
    op.create_index("ix_channel_inbox_channel_id", "channel_inbox", ["channel_id"])
    op.create_table(
        "channel_outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "inbox_id", sa.Uuid(), sa.ForeignKey("channel_inbox.id"), nullable=False, unique=True
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("parts", sa.JSON(), nullable=False),
        sa.Column("next_part", sa.Integer(), nullable=False),
        sa.Column("provider_ids", sa.JSON(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("code", sa.String(80)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('waiting','pending','sending','sent','uncertain','failed','blocked')"
        ),
    )


def downgrade():
    op.drop_table("channel_outbox")
    op.drop_table("channel_inbox")
    op.drop_table("channel_cursors")
