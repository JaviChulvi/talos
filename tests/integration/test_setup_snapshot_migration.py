"""Upgrade real legacy durable work without selecting an edited role implicitly."""

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.agents import request_lifecycle
from backend.app.applications import normalize_application
from backend.app.capabilities import compile_permissions
from backend.app.models import Agent, Operation
from worker.lifecycle import Worker

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "status, action",
    [
        ("queued", "start"),
        ("running", "apply_role"),
        ("retry_wait", "apply_role"),
    ],
)
def test_upgrade_preserves_applied_and_pending_role_snapshots(status, action):
    url = os.environ.get("TALOS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TALOS_TEST_DATABASE_URL to run PostgreSQL integration checks")
    schema = f"talos_snapshot_upgrade_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0014")
        metadata = MetaData()
        metadata.reflect(bind=engine)
        roles, employees, agents, operations = (
            metadata.tables[name] for name in ("roles", "employees", "agents", "operations")
        )
        role_id, employee_id, agent_id, operation_id = (uuid4() for _ in range(4))
        applied_role = {"id": str(role_id), "name": "Sales", "revision": 1, "capabilities": []}
        admitted_role = {**applied_role, "revision": 2, "capabilities": ["web_research"]}
        legacy_application = {
            "role": admitted_role,
            "permissions": compile_permissions(["web_research"], "openclaw"),
            "restart": action == "start",
        }
        with engine.begin() as connection:
            connection.execute(
                roles.insert().values(
                    id=role_id,
                    name="Sales",
                    description="",
                    revision=3,
                    capabilities=["terminal_execution"],
                )
            )
            connection.execute(
                employees.insert().values(
                    id=employee_id,
                    name="Alex",
                    role_id=role_id,
                )
            )
            connection.execute(
                agents.insert().values(
                    id=agent_id,
                    display_name="Agent",
                    employee_label="Alex",
                    employee_id=employee_id,
                    runtime_release="openclaw-2026.9.6",
                    runtime_mode="native",
                    desired_state="stopped",
                    observed_state="stopped",
                    revision=2,
                    applied_role=applied_role,
                )
            )
            connection.execute(
                operations.insert().values(
                    id=operation_id,
                    agent_id=agent_id,
                    action=action,
                    target_revision=2,
                    status=status,
                    step=action,
                    attempts=1,
                    idempotency_scope="legacy",
                    idempotency_key="legacy",
                    request_hash="0" * 64,
                    role_application=legacy_application,
                )
            )
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions() as session:
            agent = session.get(Agent, agent_id)
            pending = session.get(Operation, operation_id)
            assert agent.applied_application["role"] == applied_role
            assert agent.applied_application["legacy_receipt"] is True
            assert agent.selected_application["role"] == admitted_role
            assert agent.selected_application["employee_id"] == str(employee_id)
            assert "restart" not in agent.selected_application
            assert pending.role_application["permissions"] == legacy_application["permissions"]
            assert pending.role_application["restart"] == legacy_application["restart"]
            assert pending.role_application["legacy_receipt"] is True
            assert pending.role_application["employee_id"] == str(employee_id)
        worker = Worker(sessions=sessions, client=SimpleNamespace())
        worker.complete(pending, "stopped")
        with sessions() as session:
            restarted = request_lifecycle(session, agent_id, "start", "post-upgrade-start")
            selected = normalize_application(restarted.role_application, "openclaw")
            assert selected["role"]["revision"] == 2
            assert selected["role"]["capabilities"] == ["web_research"]
            assert selected["employee_id"] == str(employee_id)
            assert "legacy_receipt" not in selected
            assert (
                session.scalar(select(Operation.status).where(Operation.id == operation_id))
                == "succeeded"
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
