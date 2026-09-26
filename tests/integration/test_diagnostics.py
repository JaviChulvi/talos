import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.agents import get_db
from backend.app.diagnostics import mark_runs_stopped
from backend.app.main import create_app
from backend.app.models import Agent, Operation, Run, WorkloadIncarnation
from worker.diagnostics import DiagnosticManager
from worker.openclaw import DeliveryUncertain

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def database_engine():
    url = os.environ.get("TALOS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TALOS_TEST_DATABASE_URL to run PostgreSQL diagnostic checks")
    schema = f"talos_diagnostic_test_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def sessions(database_engine):
    with database_engine.begin() as connection:
        connection.execute(text("TRUNCATE agents, workload_incarnations, operations CASCADE"))
    return sessionmaker(database_engine, expire_on_commit=False)


@pytest.fixture
def client(sessions):
    app = create_app()

    def database():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client


@pytest.fixture
def agent_id(sessions):
    with sessions.begin() as session:
        agent = Agent(
            display_name="Diagnostic",
            employee_label="Tester",
            desired_state="running",
            observed_state="ready",
        )
        session.add(agent)
        session.flush()
        incarnation = WorkloadIncarnation(agent_id=agent.id, generation=1)
        session.add(incarnation)
        session.flush()
        agent.current_incarnation_id = incarnation.id
        return agent.id


def submit(client, agent_id, key="diagnostic-1", message="Hello"):
    return client.post(
        f"/api/v1/agents/{agent_id}/diagnostic-runs",
        json={"message": message},
        headers={"Idempotency-Key": key},
    )


def test_idempotency_and_admission(client, sessions, agent_id):
    barrier = Barrier(4)

    def call(_):
        barrier.wait(timeout=10)
        return submit(client, agent_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(call, range(4)))
    assert [r.status_code for r in responses] == [202] * 4
    assert len({r.json()["id"] for r in responses}) == 1
    run = responses[0].json()
    assert run["status"] == "queued"
    assert submit(client, agent_id, message="Different").status_code == 409
    assert submit(client, agent_id, key="other").status_code == 409
    assert submit(client, agent_id, key="blank", message="   ").status_code == 422
    assert "request_hash" not in run and "upstream_run_id" not in run
    with sessions.begin() as session:
        session.get(Agent, agent_id).desired_state = "stopped"
    # Idempotent reads still work after the lifecycle state changes.
    assert submit(client, agent_id).json()["id"] == run["id"]
    assert submit(client, agent_id, key="after-stop").status_code == 409


def test_lifecycle_and_missing_agent_admission(client, sessions, agent_id):
    assert submit(client, uuid4()).status_code == 404
    with sessions.begin() as session:
        session.add(
            Operation(
                agent_id=agent_id,
                action="start",
                target_revision=1,
                idempotency_scope="test",
                idempotency_key="pending",
                request_hash="a" * 64,
            )
        )
    assert submit(client, agent_id).status_code == 409


def test_cancel_queued_and_ordered_events(client, agent_id):
    run_id = submit(client, agent_id).json()["id"]
    path = f"/api/v1/runs/{run_id}"
    assert client.post(path + "/cancel").json()["status"] == "cancelled"
    assert client.post(path + "/cancel").json()["status"] == "cancelled"
    events = client.get(path + "/events").json()
    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["type"] for event in events] == ["queued", "cancelled"]
    assert client.get(path + "/events?after=1").json() == events[1:]
    assert client.get(path + "/events?after=2").json() == []
    assert client.get(path + "/events?after=-1").status_code == 422
    assert client.get(f"/api/v1/runs/{uuid4()}").status_code == 404
    assert submit(client, agent_id, key="next").status_code == 202


class FakeDriver:
    def __init__(self, uncertain=False, streaming=False):
        self.uncertain = uncertain
        self.streaming = streaming
        self.sent = 0
        self.aborted = 0
        self.events = asyncio.Queue()

    async def send(self, session_key, message, run_id):
        self.sent += 1
        self.run_id = run_id
        if self.uncertain:
            raise DeliveryUncertain("lost acknowledgment")
        if not self.streaming:
            for payload in (
                {"state": "delta", "deltaText": "Wrong"},
                {"state": "delta", "deltaText": "Correct", "replace": True},
                {"state": "delta", "deltaText": " answer"},
                {
                    "state": "final",
                    "message": {"content": [{"type": "text", "text": "Final answer"}]},
                },
            ):
                self.event(payload)
        return {"runId": run_id}

    def event(self, payload):
        self.events.put_nowait(
            {
                "type": "event",
                "event": "chat",
                "payload": {"runId": self.run_id, **payload},
            }
        )

    async def next_event(self, timeout):
        return await asyncio.wait_for(self.events.get(), timeout)

    async def abort(self, session_key, run_id):
        self.aborted += 1
        self.event({"state": "aborted"})
        return {"aborted": True}

    async def close(self):
        pass


def manager_for(sessions, driver):
    async def connector(_sessions, _agent_id):
        return driver

    return DiagnosticManager(sessions, connector, timeout=5)


async def drain(manager):
    await manager.tick()
    await asyncio.gather(*manager.tasks.values())
    await manager.tick()
    await manager.close()


def test_stream_replacement_and_authoritative_final(client, sessions, agent_id):
    run_id = submit(client, agent_id).json()["id"]
    driver = FakeDriver()
    asyncio.run(drain(manager_for(sessions, driver)))
    result = client.get(f"/api/v1/runs/{run_id}").json()
    assert result["status"] == "completed"
    assert result["output"] == "Final answer"
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert [e["payload"] for e in events if e["type"] == "delta"] == [
        {"text": "Wrong", "replace": False},
        {"text": "Correct", "replace": True},
        {"text": " answer", "replace": False},
    ]
    assert driver.sent == 1


def test_lost_ack_never_resends_and_stop_releases_slot(client, sessions, agent_id):
    run_id = submit(client, agent_id).json()["id"]
    driver = FakeDriver(uncertain=True)
    manager = manager_for(sessions, driver)
    asyncio.run(drain(manager))
    manager.recover()
    asyncio.run(drain(manager))
    assert driver.sent == 1
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "unknown"
    assert submit(client, agent_id, key="second").status_code == 409
    assert client.post(f"/api/v1/runs/{run_id}/cancel").json()["status"] == "unknown"
    assert submit(client, agent_id).json()["id"] == run_id
    with sessions.begin() as session:
        agent = session.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
        agent.observed_state = "stopped"
        mark_runs_stopped(session, agent_id)
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "interrupted"
    with sessions.begin() as session:
        session.get(Agent, agent_id).observed_state = "ready"
    assert submit(client, agent_id, key="after-stop").status_code == 202


def test_restart_does_not_resend_dispatch_intent(client, sessions, agent_id):
    run_id = UUID(submit(client, agent_id).json()["id"])
    with sessions.begin() as session:
        session.get(Run, run_id).status = "dispatching"
    driver = FakeDriver()
    manager = manager_for(sessions, driver)
    manager.recover()
    asyncio.run(drain(manager))
    assert driver.sent == 0
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "unknown"


def test_cancel_during_active_run_and_stop_does_not_get_overwritten(client, sessions, agent_id):
    run_id = UUID(submit(client, agent_id).json()["id"])
    driver = FakeDriver(streaming=True)
    manager = manager_for(sessions, driver)

    async def cancel():
        await manager.tick()
        while driver.sent == 0:
            await asyncio.sleep(0)
        response = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert response.json()["status"] == "cancel_requested"
        await asyncio.gather(*manager.tasks.values())
        await manager.close()

    asyncio.run(cancel())
    assert driver.aborted == 1
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "cancelled"
    # A late upstream event cannot overwrite a confirmed stop.
    with sessions.begin() as session:
        run = session.get(Run, run_id)
        run.status = "unknown"
        mark_runs_stopped(session, agent_id)
    manager._event(run_id, {"state": "final", "message": {"content": "late"}})
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "interrupted"


def test_old_incarnation_and_cancel_before_send_never_dispatch(client, sessions, agent_id):
    run_id = UUID(submit(client, agent_id).json()["id"])
    driver = FakeDriver()
    with sessions.begin() as session:
        other = WorkloadIncarnation(agent_id=agent_id, generation=2)
        session.add(other)
        session.flush()
        session.get(Agent, agent_id).current_incarnation_id = other.id
    asyncio.run(drain(manager_for(sessions, driver)))
    assert driver.sent == 0
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "interrupted"


def test_cancel_while_connecting_prevents_send(client, sessions, agent_id):
    run_id = UUID(submit(client, agent_id).json()["id"])
    driver = FakeDriver()

    async def connector(_sessions, _agent_id):
        client.post(f"/api/v1/runs/{run_id}/cancel")
        return driver

    asyncio.run(drain(DiagnosticManager(sessions, connector)))
    assert driver.sent == 0
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "cancelled"


def test_oversized_output_becomes_unknown_without_persisting_raw_payload(
    client, sessions, agent_id
):
    run_id = UUID(submit(client, agent_id).json()["id"])

    class OversizedDriver(FakeDriver):
        async def send(self, session_key, message, run_id):
            self.run_id = run_id
            self.event({"state": "delta", "deltaText": "x" * 16001})
            return {"runId": run_id}

    asyncio.run(drain(manager_for(sessions, OversizedDriver())))
    response = client.get(f"/api/v1/runs/{run_id}").json()
    assert response["status"] == "unknown"
    assert response["output"] == ""
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert not any(e["type"] == "delta" for e in events)
    assert submit(client, agent_id, key="new").status_code == 409
