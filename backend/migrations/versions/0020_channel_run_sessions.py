"""Persist employee authorization and native conversation scope per admitted turn."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "runs", sa.Column("source", sa.String(20), nullable=False, server_default="admin")
    )
    op.add_column("runs", sa.Column("session_key", sa.String(255)))
    op.add_column("runs", sa.Column("access_id", sa.Uuid()))
    op.add_column("runs", sa.Column("access_revision", sa.Integer()))
    op.add_column("runs", sa.Column("channel_revision", sa.Integer()))
    op.add_column("runs", sa.Column("employee_id", sa.Uuid()))
    op.create_foreign_key("fk_run_access", "runs", "employee_accesses", ["access_id"], ["id"])
    op.create_foreign_key("fk_run_employee", "runs", "employees", ["employee_id"], ["id"])
    op.create_check_constraint(
        "runs_source_check", "runs", "source IN ('admin','employee','probe')"
    )


def downgrade():
    # Old workers always use the admin session. Never dispatch employee/probe turns into it.
    op.execute(
        "UPDATE runs SET status='interrupted' WHERE source != 'admin' "
        "AND status IN ('queued','dispatching','running','cancel_requested','unknown')"
    )
    op.drop_constraint("runs_source_check", "runs", type_="check")
    op.drop_constraint("fk_run_employee", "runs", type_="foreignkey")
    op.drop_constraint("fk_run_access", "runs", type_="foreignkey")
    for column in (
        "employee_id",
        "channel_revision",
        "access_revision",
        "access_id",
        "session_key",
        "source",
    ):
        op.drop_column("runs", column)
