"""Retain an agent's selected image across catalog changes and restarts."""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("runtime_image", sa.String(255), nullable=True))
    op.execute(
        "UPDATE agents AS agent SET runtime_image = incarnation.image_digest "
        "FROM workload_incarnations AS incarnation "
        "WHERE agent.current_incarnation_id = incarnation.id "
        "AND agent.runtime_mode = 'native' "
        "AND agent.runtime_release = incarnation.runtime_release"
    )


def downgrade():
    op.drop_column("agents", "runtime_image")
