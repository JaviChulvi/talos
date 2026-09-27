"""Persist the selected model and each run's model snapshot."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workload_incarnations",
        sa.Column("model_route", sa.String(20), nullable=False, server_default="fixture"),
    )
    op.add_column(
        "runs", sa.Column("model_id", sa.String(255), nullable=False, server_default="fixture")
    )
    config = op.create_table(
        "inference_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.CheckConstraint("id = 1"),
    )
    op.bulk_insert(config, [{"id": 1, "model_id": "fixture"}])


def downgrade():
    op.drop_table("inference_config")
    op.drop_column("runs", "model_id")
    op.drop_column("workload_incarnations", "model_route")
