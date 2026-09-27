"""Employee identities and reusable role capability assignments."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0"),
    )
    op.create_table(
        "employees",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("email", sa.String(254)),
        sa.Column("role_id", sa.Uuid(), sa.ForeignKey("roles.id"), nullable=False),
    )
    op.create_index("ix_employees_role_id", "employees", ["role_id"])
    op.add_column("agents", sa.Column("employee_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_agents_employee", "agents", "employees", ["employee_id"], ["id"])
    op.create_index("ix_agents_employee_id", "agents", ["employee_id"])


def downgrade():
    op.drop_index("ix_agents_employee_id", table_name="agents")
    op.drop_constraint("fk_agents_employee", "agents", type_="foreignkey")
    op.drop_column("agents", "employee_id")
    op.drop_table("employees")
    op.drop_table("roles")
