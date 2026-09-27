"""Native OpenClaw instances and short-lived dashboard handoffs."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agents", sa.Column("runtime_mode", sa.String(20), nullable=False, server_default="managed")
    )
    op.add_column("operations", sa.Column("dashboard_url", sa.Text(), nullable=True))
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create', 'start', 'stop', 'delete', 'dashboard')",
    )


def downgrade():
    op.execute("DELETE FROM operations WHERE action = 'dashboard'")
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check", "operations", "action IN ('create', 'start', 'stop', 'delete')"
    )
    op.drop_column("operations", "dashboard_url")
    op.drop_column("agents", "runtime_mode")
