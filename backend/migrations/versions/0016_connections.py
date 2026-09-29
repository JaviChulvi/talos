"""Connection metadata and immutable references to private credential files."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "roles", sa.Column("connection_bindings", sa.JSON(), nullable=False, server_default="{}")
    )
    op.add_column(
        "employees",
        sa.Column("connection_overrides", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "connection_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("connection_id", "version"),
    )
    op.create_index(
        "ix_connection_versions_connection_id", "connection_versions", ["connection_id"]
    )
    op.create_foreign_key(
        "fk_connection_current_version",
        "connections",
        "connection_versions",
        ["current_version_id"],
        ["id"],
    )


def downgrade():
    op.drop_column("employees", "connection_overrides")
    op.drop_column("roles", "connection_bindings")
    op.drop_constraint("fk_connection_current_version", "connections", type_="foreignkey")
    op.drop_table("connection_versions")
    op.drop_table("connections")
