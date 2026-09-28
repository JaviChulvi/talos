"""Portable setup drafts and immutable published revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "setups",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("draft_manifest", sa.JSON(), nullable=False),
        sa.Column("draft_artifact_hash", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "setup_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("setup_id", sa.Uuid(), sa.ForeignKey("setups.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("setup_id", "version", name="uq_setup_revision_version"),
        sa.CheckConstraint("version > 0"),
    )
    op.create_index("ix_setup_revisions_setup_id", "setup_revisions", ["setup_id"])


def downgrade():
    op.drop_table("setup_revisions")
    op.drop_table("setups")
