"""Admin-managed channel identities and single-use access invitations."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "connections", sa.Column("purpose", sa.String(20), nullable=False, server_default="tools")
    )
    op.create_check_constraint(
        "connections_purpose_check", "connections", "purpose IN ('tools','channel')"
    )
    op.create_table(
        "employee_channels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "connection_id", sa.Uuid(), sa.ForeignKey("connections.id"), nullable=False, unique=True
        ),
        sa.Column("workspace_id", sa.String(40), nullable=False),
        sa.Column("verified_version_id", sa.Uuid(), sa.ForeignKey("connection_versions.id")),
        sa.Column("identity", sa.JSON(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("provider IN ('telegram','slack')"),
        sa.CheckConstraint("revision > 0"),
    )
    op.create_table(
        "employee_accesses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("employee_channels.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "employee_id", sa.Uuid(), sa.ForeignKey("employees.id"), nullable=False, index=True
        ),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agents.id"), nullable=False, index=True),
        sa.Column("external_scope", sa.String(40), nullable=False),
        sa.Column("external_user_id", sa.String(40)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("channel_id", "employee_id"),
        sa.UniqueConstraint("channel_id", "external_scope", "external_user_id"),
        sa.CheckConstraint("state IN ('pending','active','disabled')"),
        sa.CheckConstraint("revision > 0"),
    )
    op.create_table(
        "access_invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "access_id",
            sa.Uuid(),
            sa.ForeignKey("employee_accesses.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("access_revision", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("access_invitations")
    op.drop_table("employee_accesses")
    op.drop_table("employee_channels")
    # Old code has no channel-secret classification; do not expose these as tool connections.
    op.execute("UPDATE connections SET current_version_id = NULL WHERE purpose = 'channel'")
    op.execute(
        "DELETE FROM connection_versions WHERE connection_id IN "
        "(SELECT id FROM connections WHERE purpose = 'channel')"
    )
    op.execute("DELETE FROM connections WHERE purpose = 'channel'")
    op.drop_constraint("connections_purpose_check", "connections", type_="check")
    op.drop_column("connections", "purpose")
