"""Optional employee allowances, unlimited by default."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("employees", sa.Column("monthly_allowance_usd", sa.Numeric(24, 12)))
    op.create_check_constraint("ck_employee_allowance", "employees", "monthly_allowance_usd >= 0")


def downgrade():
    op.drop_constraint("ck_employee_allowance", "employees")
    op.drop_column("employees", "monthly_allowance_usd")
