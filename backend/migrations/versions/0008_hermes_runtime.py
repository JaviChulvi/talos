"""Choose OpenClaw or Hermes for native instances."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agents",
        sa.Column("runtime_kind", sa.String(20), nullable=False, server_default="openclaw"),
    )
    op.add_column("agents", sa.Column("dashboard_password_hash", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("agents", "dashboard_password_hash")
    op.drop_column("agents", "runtime_kind")
