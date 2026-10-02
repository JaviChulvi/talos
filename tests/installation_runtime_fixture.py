"""Executed through the installed API container, against its real disposable database.

Uses the production agent creation/lifecycle owners and actual worker. No provider
request is needed to start a native runtime; no external channel credentials exist.
"""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from backend.app.agents import CreateAgent, create_agent, request_lifecycle
from backend.app.auth import bootstrap
from backend.app.config import get_settings
from backend.app.connections import Connection
from backend.app.db import session_factory
from backend.app.models import (
    AdministratorSession,
    Agent,
    Employee,
    EmployeeChannel,
    Operation,
    Role,
    Run,
)

FACTORY = session_factory()


def wait_operation(operation_id):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        with FACTORY() as session:
            operation = session.get(Operation, operation_id)
            if operation.status == "succeeded":
                return
            if operation.status == "failed":
                raise RuntimeError("Real native lifecycle failed: " + str(operation.error))
        time.sleep(1)
    raise TimeoutError("Real native lifecycle did not complete")


def lifecycle(agent_id, action):
    with FACTORY() as session:
        operation = request_lifecycle(session, agent_id, action, uuid4().hex)
        operation_id = operation.id
    wait_operation(operation_id)


def inventory():
    settings = get_settings()
    with FACTORY() as session:
        agents = session.scalars(select(Agent).where(Agent.display_name.like("acceptance-%"))).all()
        return [
            {
                "id": str(agent.id),
                "name": agent.display_name,
                "desired_state": agent.desired_state,
                "observed_state": agent.observed_state,
                "incarnation": str(agent.current_incarnation_id),
                "container": agent.current_incarnation.container_id,
                "image": agent.current_incarnation.image_digest,
                "state_volume": f"talos-{settings.compose_project}-{settings.installation_id}"
                f"-{agent.id.hex}-state",
                "config_volume": agent.current_incarnation.config_volume,
                "runs": [
                    {"id": str(run.id), "status": run.status, "output": run.output}
                    for run in session.scalars(select(Run).where(Run.agent_id == agent.id))
                ],
            }
            for agent in agents
        ]


def bootstrap_fixture():
    with FACTORY() as session:
        bootstrap(session, "synthetic-acceptance-admin-password")
    with FACTORY.begin() as session:
        session.add(
            AdministratorSession(
                token_hash="b" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        for provider in ("telegram", "slack"):
            # Missing credentials prevent any transport from contacting a real service.
            connection = Connection(name="Acceptance " + provider, purpose="channel")
            session.add(connection)
            session.flush()
            session.add(
                EmployeeChannel(
                    provider=provider,
                    name="Synthetic " + provider,
                    enabled=True,
                    connection_id=connection.id,
                )
            )
    return {"administrator_created": True}


def seed():
    with FACTORY.begin() as session:
        role = Role(name="Acceptance employee role", capabilities=[])
        session.add(role)
        session.flush()
        employee = Employee(name="Synthetic installation employee", role_id=role.id)
        session.add(employee)
        session.flush()
        employee_id = employee.id
    for family in ("openclaw", "hermes"):
        for state in ("running", "stopped", "uncertain"):
            body = CreateAgent(
                runtime_kind=family,
                runtime_mode="native",
                employee_id=employee_id,
                display_name=f"acceptance-{family}-{state}",
                dashboard_password="synthetic-acceptance-password" if family == "hermes" else None,
            )
            with FACTORY() as session:
                operation = asyncio.run(create_agent(body, uuid4().hex, session))
                agent_id, operation_id = operation.agent_id, operation.id
            wait_operation(operation_id)
            lifecycle(agent_id, "start")
            if state == "stopped":
                lifecycle(agent_id, "stop")
            elif state == "uncertain":
                with FACTORY.begin() as session:
                    agent = session.get(Agent, agent_id)
                    session.add(
                        Run(
                            agent_id=agent_id,
                            incarnation_id=agent.current_incarnation_id,
                            source="admin",
                            status="unknown",
                            message="synthetic uncertain action",
                            idempotency_key=uuid4().hex,
                            request_hash="a" * 64,
                            output="external outcome intentionally unresolved",
                        )
                    )
    return inventory()


def stop():
    with FACTORY() as session:
        ids = list(
            session.scalars(
                select(Agent.id).where(
                    Agent.display_name.like("acceptance-%"), Agent.desired_state == "running"
                )
            )
        )
    for agent_id in ids:
        lifecycle(agent_id, "stop")
    return inventory()


if __name__ == "__main__":
    print(
        json.dumps(
            {"bootstrap": bootstrap_fixture, "seed": seed, "inventory": inventory, "stop": stop}[
                sys.argv[1]
            ]()
        )
    )
