"""Rename identities and configuration profiles without changing their ownership."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

TABLES = (
    ("roles", "profiles"),
    ("employees", "users"),
    ("employee_channels", "user_channels"),
    ("employee_accesses", "user_accesses"),
)
# Table names here refer to the upgraded schema.
COLUMNS = (
    ("users", "role_id", "profile_id"),
    ("users", "monthly_allowance_usd", "monthly_budget_usd"),
    ("agents", "employee_id", "user_id"),
    ("agents", "employee_label", "user_label"),
    ("agents", "applied_role", "applied_profile"),
    ("user_accesses", "employee_id", "user_id"),
    ("runs", "employee_id", "user_id"),
    ("inference_calls", "employee_id", "user_id"),
    ("operations", "role_application", "profile_application"),
)
CONSTRAINTS = {
    "agents": ("fk_agents_employee",),
    "runs": ("fk_run_employee",),
    "profiles": ("fk_role_setup_revision", "roles_name_key", "roles_pkey", "roles_revision_check"),
    "users": ("ck_employee_allowance", "employees_pkey", "employees_role_id_fkey"),
    "inference_calls": ("inference_calls_employee_id_fkey",),
    "user_channels": (
        "employee_channels_connection_id_fkey",
        "employee_channels_connection_id_key",
        "employee_channels_pkey",
        "employee_channels_provider_check",
        "employee_channels_provider_key",
        "employee_channels_revision_check",
        "employee_channels_verified_version_id_fkey",
    ),
    "user_accesses": (
        "employee_accesses_agent_id_fkey",
        "employee_accesses_channel_id_employee_id_key",
        "employee_accesses_channel_id_external_scope_external_user_i_key",
        "employee_accesses_channel_id_fkey",
        "employee_accesses_employee_id_fkey",
        "employee_accesses_pkey",
        "employee_accesses_revision_check",
        "employee_accesses_state_check",
    ),
}
INDEXES = (
    "ix_agents_employee_id",
    "ix_employees_role_id",
    "ix_employee_accesses_agent_id",
    "ix_employee_accesses_channel_id",
    "ix_employee_accesses_employee_id",
    "ix_inference_calls_employee_month",
)


def modern_name(name):
    return (
        name.replace("employee", "user").replace("role", "profile").replace("allowance", "budget")
    )


def rename_metadata(*, reverse=False):
    for table, names in CONSTRAINTS.items():
        for old in names:
            new = modern_name(old)
            before, after = (new, old) if reverse else (old, new)
            op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{before}" TO "{after}"')
    for old in INDEXES:
        new = modern_name(old)
        before, after = (new, old) if reverse else (old, new)
        op.execute(f'ALTER INDEX "{before}" RENAME TO "{after}"')


def rename_snapshots(*, reverse=False):
    # Only Talos-owned top-level keys change. User-authored setup manifests,
    # credentials, names, instructions and native receipts remain untouched.
    pairs = (("role", "profile"), ("employee_id", "user_id"))
    bind = op.get_bind()
    for table, column in (
        ("agents", "selected_application"),
        ("agents", "applied_application"),
        ("operations", "profile_application"),
    ):
        records = sa.table(table, sa.column("id", sa.Uuid()), sa.column(column, sa.JSON()))
        snapshots = sa.select(records.c.id, records.c[column]).where(
            sa.func.json_typeof(records.c[column]) == "object"
        )
        # JSONB rejects accepted JSON content such as escaped nulls in instructions.
        for identifier, snapshot in bind.execute(snapshots.execution_options(yield_per=100)):
            for old, new in pairs:
                before, after = (new, old) if reverse else (old, new)
                if before in snapshot:
                    snapshot[after] = snapshot.pop(before)
            bind.execute(
                records.update().where(records.c.id == identifier).values({column: snapshot})
            )


def rename_values(*, reverse=False):
    old_action, new_action = ("apply_role", "apply_profile")
    old_source, new_source = ("employee", "user")
    if reverse:
        old_action, new_action = new_action, old_action
        old_source, new_source = new_source, old_source
    op.drop_constraint("operations_action_check", "operations", type_="check")
    op.drop_constraint("runs_source_check", "runs", type_="check")
    bind = op.get_bind()
    # Replay identity follows the new public action, including completed operations.
    rows = bind.execute(
        sa.text("SELECT id, agent_id, idempotency_scope FROM operations WHERE action = :action"),
        {"action": old_action},
    ).mappings()
    for row in rows:
        if row["idempotency_scope"] != f"agent:{row['agent_id']}:{old_action}":
            continue
        payload = {"agent_id": str(row["agent_id"]), "action": new_action}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE operations SET idempotency_scope=:scope, request_hash=:digest WHERE id=:id"
            ),
            {"scope": f"agent:{row['agent_id']}:{new_action}", "digest": digest, "id": row["id"]},
        )
    bind.execute(
        sa.text("UPDATE operations SET action=:new WHERE action=:old"),
        {"old": old_action, "new": new_action},
    )
    bind.execute(
        sa.text("UPDATE operations SET step=:new WHERE step=:old"),
        {"old": old_action, "new": new_action},
    )
    bind.execute(
        sa.text("UPDATE runs SET source=:new WHERE source=:old"),
        {"old": old_source, "new": new_source},
    )
    for old, new in (
        ("employee_assignment_required", "user_assignment_required"),
        ("allowance_exhausted", "budget_exhausted"),
        ("allowance_available", "budget_available"),
        ("allowance_or_credit_exhausted", "budget_or_credit_exhausted"),
    ):
        before, after = (new, old) if reverse else (old, new)
        bind.execute(
            sa.text("UPDATE availability_checks SET code=:new WHERE code=:old"),
            {"old": before, "new": after},
        )
    op.create_check_constraint(
        "operations_action_check",
        "operations",
        "action IN ('create','start','stop','delete','dashboard',"
        f"'{new_action}','configure_model','capture_setup')",
    )
    op.create_check_constraint(
        "runs_source_check", "runs", f"source IN ('admin','{new_source}','probe')"
    )


def upgrade():
    for old, new in TABLES:
        op.rename_table(old, new)
    for table, old, new in COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    rename_metadata()
    rename_snapshots()
    rename_values()


def downgrade():
    rename_values(reverse=True)
    rename_snapshots(reverse=True)
    rename_metadata(reverse=True)
    for table, old, new in reversed(COLUMNS):
        op.alter_column(table, new, new_column_name=old)
    for old, new in reversed(TABLES):
        op.rename_table(new, old)
