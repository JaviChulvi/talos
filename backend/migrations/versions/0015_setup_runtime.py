"""Pin role setups and retain explicit application selections across restarts."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("roles", sa.Column("setup_revision_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_role_setup_revision", "roles", "setup_revisions", ["setup_revision_id"], ["id"]
    )
    op.add_column(
        "roles", sa.Column("connector_grants", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column("agents", sa.Column("selected_application", sa.JSON(), nullable=True))
    op.add_column("agents", sa.Column("applied_application", sa.JSON(), nullable=True))
    op.execute("""UPDATE agents SET selected_application = json_build_object(
        'role', applied_role, 'employee_id', employee_id::text, 'setup', NULL,
        'connector_grants', '[]'::json, 'connections', '{}'::json, 'legacy_receipt', true),
        applied_application = json_build_object(
        'role', applied_role, 'employee_id', employee_id::text, 'setup', NULL,
        'connector_grants', '[]'::json, 'connections', '{}'::json, 'legacy_receipt', true)
        WHERE applied_role IS NOT NULL AND applied_role::text != 'null'""")
    # Durable work admitted before this migration already captured its role policy.
    # Retain that snapshot, attach its employee identity, and grandfather its lack
    # of a setup receipt rather than selecting the role again after an upgrade.
    op.execute("""UPDATE operations AS operation SET role_application =
        (operation.role_application::jsonb || jsonb_build_object(
            'employee_id', agent.employee_id::text, 'setup', NULL,
            'connector_grants', '[]'::jsonb, 'connections', '{}'::jsonb,
            'legacy_receipt', true))::json
        FROM agents AS agent
        WHERE operation.agent_id = agent.id
            AND operation.status IN ('queued', 'running', 'retry_wait')
            AND operation.role_application IS NOT NULL
            AND operation.role_application::text != 'null'""")
    op.execute("""UPDATE agents AS agent SET selected_application =
        (operation.role_application::jsonb - 'restart')::json
        FROM operations AS operation
        WHERE operation.agent_id = agent.id
            AND operation.status IN ('queued', 'running', 'retry_wait')
            AND operation.role_application IS NOT NULL
            AND operation.role_application::text != 'null'""")


def downgrade():
    op.drop_column("agents", "applied_application")
    op.drop_column("agents", "selected_application")
    op.drop_column("roles", "connector_grants")
    op.drop_constraint("fk_role_setup_revision", "roles", type_="foreignkey")
    op.drop_column("roles", "setup_revision_id")
