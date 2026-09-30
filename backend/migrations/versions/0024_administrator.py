"""Singleton administrator and revocable browser sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "administrator",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1"),
        sa.CheckConstraint("failed_attempts BETWEEN 0 AND 5"),
    )
    op.create_table(
        "administrator_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "administrator_id", sa.Integer(), sa.ForeignKey("administrator.id"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("administrator_sessions")
    op.drop_table("administrator")
