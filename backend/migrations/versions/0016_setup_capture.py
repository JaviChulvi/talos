"""Durable capture operations and review metadata for setup drafts."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("setups", sa.Column("capture_metadata", sa.JSON(), nullable=True))
    op.add_column("operations", sa.Column("result", sa.JSON(), nullable=True))
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create','start','stop','delete','dashboard',"
        "'apply_role','configure_model','capture_setup')",
    )


def downgrade():
    op.execute("DELETE FROM operations WHERE action = 'capture_setup'")
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create','start','stop','delete','dashboard','apply_role','configure_model')",
    )
    op.drop_column("operations", "result")
    op.drop_column("setups", "capture_metadata")
