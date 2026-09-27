"""Apply native model choices through the existing lifecycle worker."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operations", sa.Column("model_selection", sa.JSON(), nullable=True))
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create', 'start', 'stop', 'delete', 'dashboard', "
        "'apply_role', 'configure_model')",
    )


def downgrade():
    op.execute("DELETE FROM operations WHERE action = 'configure_model'")
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create', 'start', 'stop', 'delete', 'dashboard', 'apply_role')",
    )
    op.drop_column("operations", "model_selection")
