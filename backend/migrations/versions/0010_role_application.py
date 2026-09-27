"""Capture and record native role applications."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("applied_role", sa.JSON(), nullable=True))
    op.add_column("operations", sa.Column("role_application", sa.JSON(), nullable=True))
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create', 'start', 'stop', 'delete', 'dashboard', 'apply_role')",
    )


def downgrade():
    op.execute("DELETE FROM operations WHERE action = 'apply_role'")
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create', 'start', 'stop', 'delete', 'dashboard')",
    )
    op.drop_column("operations", "role_application")
    op.drop_column("agents", "applied_role")
