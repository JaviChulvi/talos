"""Optional per-agent inference configuration; null inherits workspace defaults."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("inference_override", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("agents", "inference_override")
