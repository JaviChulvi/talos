"""Upgrade populated records and replay identities, then reverse without losing data."""

import hashlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.app.agents import CreateAgent, create_agent, request_lifecycle
from backend.app.applications import application_fingerprint
from backend.app.availability import agent_fingerprint
from backend.app.models import Agent, InferenceCall, Operation, Run, User, UserAccess, UserChannel
from backend.app.usage import user_budget
from worker.setup_runtime import application_fingerprint as runtime_fingerprint

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def digest(payload, *, compact=True):
    options = {"separators": (",", ":")} if compact else {}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, **options).encode()).hexdigest()


@pytest.mark.parametrize("instructions", ["Keep my notes", "Keep my \x00 notes"])
def test_populated_upgrade_preserves_budget_channels_receipts_and_replays(instructions):
    import asyncio

    url = os.environ.get("TALOS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TALOS_TEST_DATABASE_URL to run PostgreSQL integration checks")
    schema = f"talos_terminology_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    ids = {
        name: uuid4()
        for name in (
            "profile",
            "user",
            "agent",
            "operation",
            "create",
            "run",
            "call",
            "incarnation",
            "channel",
            "access",
            "connection",
        )
    }
    now = datetime.now(UTC)
    profile = {"id": str(ids["profile"]), "name": "Research", "revision": 1, "capabilities": []}
    application = {
        "role": profile,
        "employee_id": str(ids["user"]),
        "setup": {
            "manifest": {
                "description": "employee role",
                "role": "user-authored",
                "instructions": instructions,
            }
        },
        "connections": {},
    }
    application["fingerprint"] = digest(application)
    old_fingerprint = digest(
        {
            "revision": 1,
            "incarnation": str(ids["incarnation"]),
            "employee": str(ids["user"]),
            "runtime": "openclaw-2026.9.6",
            "application": application,
            "selected": application,
            "model": None,
            "default_model": None,
        },
        compact=False,
    )
    session_key = f"agent:main:employee:telegram:{ids['access']}:1:{ids['agent']}"
    old_request = {
        "display_name": "Research helper",
        "employee_label": "Alex",
        "employee_id": str(ids["user"]),
        "runtime_mode": "native",
    }
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0026")
            metadata = MetaData()
            metadata.reflect(bind=connection)

            def insert(table, **values):
                connection.execute(metadata.tables[table].insert().values(**values))

            insert(
                "roles",
                id=ids["profile"],
                name="Research",
                description="",
                capabilities=[],
                revision=1,
            )
            insert(
                "employees",
                id=ids["user"],
                name="Alex",
                role_id=ids["profile"],
                monthly_allowance_usd=Decimal("25"),
            )
            insert(
                "agents",
                id=ids["agent"],
                display_name="Research helper",
                employee_label="Alex",
                employee_id=ids["user"],
                runtime_release="openclaw-2026.9.6",
                runtime_mode="native",
                desired_state="stopped",
                observed_state="stopped",
                revision=1,
                applied_role=profile,
                selected_application=application,
                applied_application=application,
            )
            insert(
                "workload_incarnations",
                id=ids["incarnation"],
                agent_id=ids["agent"],
                generation=1,
                runtime_release="openclaw-2026.9.6",
                model_route="native",
            )
            connection.execute(
                metadata.tables["agents"].update().values(current_incarnation_id=ids["incarnation"])
            )
            for name, action, scope, request in (
                (
                    "operation",
                    "apply_role",
                    f"agent:{ids['agent']}:apply_role",
                    {"agent_id": str(ids["agent"]), "action": "apply_role"},
                ),
                ("create", "create", "create-agent", old_request),
            ):
                insert(
                    "operations",
                    id=ids[name],
                    agent_id=ids["agent"],
                    action=action,
                    status="succeeded",
                    target_revision=1,
                    attempts=1,
                    step=action,
                    idempotency_scope=scope,
                    idempotency_key=name,
                    request_hash=digest(request),
                    role_application={**application, "restart": False}
                    if name == "operation"
                    else None,
                )
            insert(
                "connections",
                id=ids["connection"],
                name="Private channel",
                description="",
                fields=["bot_token"],
                current_version=0,
                purpose="channel",
            )
            insert(
                "employee_channels",
                id=ids["channel"],
                provider="telegram",
                name="Personal",
                enabled=True,
                revision=1,
                connection_id=ids["connection"],
                workspace_id="",
                identity={},
            )
            insert(
                "employee_accesses",
                id=ids["access"],
                channel_id=ids["channel"],
                employee_id=ids["user"],
                agent_id=ids["agent"],
                external_scope="",
                external_user_id="123456789",
                state="active",
                revision=1,
            )
            insert(
                "runs",
                id=ids["run"],
                agent_id=ids["agent"],
                incarnation_id=ids["incarnation"],
                source="employee",
                employee_id=ids["user"],
                session_key=session_key,
                access_id=ids["access"],
                access_revision=1,
                channel_revision=1,
                message="Keep my employee role notes",
                output="Saved",
                status="completed",
                cancel_requested=False,
                idempotency_key="turn",
                request_hash="a" * 64,
                event_count=0,
                availability_fingerprint=old_fingerprint,
            )
            insert(
                "inference_calls",
                id=ids["call"],
                agent_id=ids["agent"],
                incarnation_id=ids["incarnation"],
                employee_id=ids["user"],
                run_id=ids["run"],
                model="test/model",
                cost_usd=Decimal("1.25"),
                outcome="completed",
                admitted_at=now,
            )
            command.upgrade(config, "head")
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions() as session:
            user = session.get(User, ids["user"])
            assert user.profile.id == ids["profile"]
            assert user.monthly_budget_usd == Decimal("25")
            assert user_budget(session, user, now)["known_spend_usd"] == "1.250000000000"
            agent = session.get(Agent, ids["agent"])
            assert agent.user_name == "Alex" and agent.applied_profile == profile
            assert agent.selected_application["profile"] == profile
            assert agent.selected_application["user_id"] == str(user.id)
            assert "role" not in agent.selected_application
            assert agent.selected_application["setup"] == application["setup"]
            assert application_fingerprint(agent.applied_application) == application["fingerprint"]
            assert runtime_fingerprint(agent.applied_application) == application["fingerprint"]
            assert agent_fingerprint(session, agent) == old_fingerprint
            run = session.get(Run, ids["run"])
            assert run.source == "user" and run.user_id == user.id
            assert run.session_key == session_key and run.message == "Keep my employee role notes"
            assert session.get(InferenceCall, ids["call"]).user_id == user.id
            assert session.get(UserChannel, ids["channel"]).enabled
            assert session.get(UserAccess, ids["access"]).state == "active"
            operation = session.get(Operation, ids["operation"])
            assert operation.action == operation.step == "apply_profile"
            assert operation.profile_application == {**agent.applied_application, "restart": False}
        with sessions() as session:
            assert (
                request_lifecycle(session, ids["agent"], "apply_profile", "operation").id
                == ids["operation"]
            )
        with sessions() as session:
            body = CreateAgent(
                display_name="Research helper", user_label="Alex", user_id=ids["user"]
            )
            assert asyncio.run(create_agent(body, "create", session)).id == ids["create"]
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0026")
            assert connection.execute(
                text("SELECT monthly_allowance_usd FROM employees")
            ).scalar_one() == Decimal("25")
            for column in ("selected_application", "applied_application"):
                assert (
                    connection.execute(text(f"SELECT {column} FROM agents")).scalar_one()
                    == application
                )
            assert connection.execute(
                text("SELECT role_application FROM operations WHERE action='apply_role'")
            ).scalar_one() == {**application, "restart": False}
            assert connection.execute(text("SELECT source FROM runs")).scalar_one() == "employee"
            assert connection.execute(
                text("SELECT request_hash FROM operations WHERE action='apply_role'")
            ).scalar_one() == digest({"agent_id": str(ids["agent"]), "action": "apply_role"})
            command.upgrade(config, "head")
            assert {"users", "profiles", "user_channels", "user_accesses"} <= set(
                inspect(connection).get_table_names()
            )
            for table in inspect(connection).get_table_names():
                assert not any(
                    "employee" in c["name"] or "role" in c["name"] or "allowance" in c["name"]
                    for c in inspect(connection).get_columns(table)
                )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
