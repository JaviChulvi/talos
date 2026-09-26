"""Snapshot model capabilities and overrides; retain per-call usage and outcome."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    for table, names in (
        ("inference_config", ("settings", "capabilities")),
        ("runs", ("inference",)),
    ):
        for name in names:
            op.add_column(table, sa.Column(name, sa.JSON(), nullable=False, server_default="{}"))
    op.add_column(
        "runs", sa.Column("inference_calls", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade():
    op.drop_column("runs", "inference_calls")
    op.drop_column("runs", "inference")
    op.drop_column("inference_config", "capabilities")
    op.drop_column("inference_config", "settings")
