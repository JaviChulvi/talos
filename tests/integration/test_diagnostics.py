import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
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
    assert submit(client, agent_id, message="A\x00B").status_code == 422
    with sessions() as session:
        assert session.scalars(select(Run)).all() == []
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


@pytest.mark.parametrize("runtime_kind", ["openclaw", "hermes"])
def test_native_chat_keeps_runtime_configuration(client, sessions, agent_id, runtime_kind):
    from worker.lifecycle import configure_inference

    with sessions.begin() as session:
        agent = session.get(Agent, agent_id)
        agent.runtime_mode, agent.runtime_kind = "native", runtime_kind
        session.get(WorkloadIncarnation, agent.current_incarnation_id).model_route = "native"
    response = submit(client, agent_id)
    assert response.status_code == 202
    run = response.json()
    assert run["model_id"] == "native" and run["inference"] == {"source": "native"}
    assert submit(client, agent_id).json()["id"] == run["id"]
    assert submit(client, agent_id, key="concurrent").status_code == 409
    assert client.get(f"/api/v1/agents/{agent_id}/runs").json() == [run]
    assert client.get(f"/api/v1/agents/{uuid4()}/runs").status_code == 404
    runtime = AsyncMock()
    with sessions() as session:
        # Only the Talos session selection changes; native configuration stays owned by the agent.
        asyncio.run(configure_inference(sessions, session.get(Run, UUID(run["id"])), runtime))
    if runtime_kind == "openclaw":
        runtime.request.assert_awaited_once_with(
            "sessions.patch", {"key": f"agent:main:talos:{agent_id}", "model": None}
        )
    else:
        assert runtime.model_id is None
        runtime.request.assert_not_called()


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
    from worker.diagnostics import MAX_OUTPUT

    run_id = UUID(submit(client, agent_id).json()["id"])

    class OversizedDriver(FakeDriver):
        async def send(self, session_key, message, run_id):
            self.run_id = run_id
            self.event({"state": "delta", "deltaText": "x" * (MAX_OUTPUT + 1)})
            return {"runId": run_id}

    asyncio.run(drain(manager_for(sessions, OversizedDriver())))
    response = client.get(f"/api/v1/runs/{run_id}").json()
    assert response["status"] == "unknown"
    assert response["output"] == ""
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert not any(e["type"] == "delta" for e in events)
    assert submit(client, agent_id, key="new").status_code == 409


@pytest.mark.parametrize("failure_point", ["_claim", "_can_send", "_ack"])
def test_database_recovery_respects_durable_send_intent(
    client, sessions, agent_id, monkeypatch, failure_point
):
    run_id = UUID(submit(client, agent_id).json()["id"])
    driver = FakeDriver()
    manager = manager_for(sessions, driver)
    original = getattr(manager, failure_point)

    def fail_once(*args):
        monkeypatch.setattr(manager, failure_point, original)
        raise OperationalError("fixture", {}, OSError("Database restarting"))

    monkeypatch.setattr(manager, failure_point, fail_once)

    async def scenario():
        await manager.tick()
        await asyncio.gather(*manager.tasks.values(), return_exceptions=True)
        with pytest.raises(OperationalError):
            await manager.tick()
        assert run_id not in manager.tasks
        # After the database returns, only an unclaimed queued run may dispatch.
        await manager.tick()
        await asyncio.gather(*manager.tasks.values())
        await manager.tick()
        await manager.tick()
        await manager.close()

    asyncio.run(scenario())
    status = "completed" if failure_point == "_claim" else "unknown"
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == status
    assert driver.sent == (0 if failure_point == "_can_send" else 1)


def test_recovery_preserves_a_live_streaming_task(client, sessions, agent_id):
    run_id = UUID(submit(client, agent_id).json()["id"])
    driver = FakeDriver(streaming=True)
    manager = manager_for(sessions, driver)

    async def scenario():
        await manager.tick()
        while driver.sent == 0:
            await asyncio.sleep(0)
        await manager.tick()
        assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "running"
        driver.event({"state": "final", "message": {"content": "Completed"}})
        await asyncio.gather(*manager.tasks.values())
        await manager.tick()
        await manager.close()

    asyncio.run(scenario())
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "completed"
    assert driver.sent == 1
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert not any(event["type"] == "unknown" for event in events)


def test_model_selection_snapshots_requests_and_old_runtimes_stay_fixture(
    client, sessions, agent_id, monkeypatch
):
    from backend.app.models import InferenceConfig

    async def catalog():
        return [
            {
                "id": "deepseek/deepseek-v4-flash-0731",
                "name": "DeepSeek",
                "context_length": 64000,
                "max_completion_tokens": 8000,
                "supported_parameters": ["reasoning", "max_tokens"],
                "reasoning": {"mandatory": True, "supported_efforts": ["low", "high"]},
            },
            {"id": "other/model", "name": "Other"},
        ]

    monkeypatch.setattr("backend.app.inference.catalog", catalog)
    first = "deepseek/deepseek-v4-flash-0731"
    assert client.put("/api/v1/inference", json={"model_id": "unknown"}).status_code == 400
    assert client.put("/api/v1/inference", json={"model_id": first}).status_code == 200
    assert (
        client.put(
            "/api/v1/inference", json={"model_id": first, "settings": {"reasoning_effort": "none"}}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/inference", json={"model_id": first, "settings": {"max_output_tokens": 9000}}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/v1/inference", json={"model_id": first, "settings": {"temperature": 1}}
        ).status_code
        == 400
    )
    overrides = {"reasoning_effort": "high", "max_output_tokens": 7000}
    assert (
        client.put("/api/v1/inference", json={"model_id": first, "settings": overrides}).status_code
        == 200
    )
    # A pre-upgrade incarnation keeps its existing fixture configuration until stop/start.
    assert submit(client, agent_id).json()["model_id"] == "fixture"
    with sessions.begin() as session:
        run = session.scalar(select(Run))
        run.status = "completed"
        incarnation = session.get(WorkloadIncarnation, run.incarnation_id)
        incarnation.model_route = "default"
    response = submit(client, agent_id, key="real-model")
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert response.json()["model_id"] == first
    assert response.json()["inference"]["settings"] == overrides
    assert client.put("/api/v1/inference", json={"model_id": "other/model"}).status_code == 200
    assert client.get(f"/api/v1/runs/{run_id}").json()["model_id"] == first
    assert client.get(f"/api/v1/runs/{run_id}").json()["inference"]["settings"] == overrides
    with sessions.begin() as session:
        session.get(Run, UUID(run_id)).status = "completed"
    assert submit(client, agent_id, key="next-model").json()["model_id"] == "other/model"
    # Restore the singleton for other tests sharing this isolated schema.
    with sessions.begin() as session:
        session.get(InferenceConfig, 1).model_id = "fixture"


def test_gateway_requires_admission_and_revokes_on_cancel(sessions, agent_id, monkeypatch):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from gateway.identity import admit_inference, record_inference, selected_request, validate_token

    ledger_identity(sessions, agent_id, monkeypatch)
    token = "test-workload-token-at-least-twenty-chars"
    monkeypatch.setattr("gateway.identity.session_factory", lambda: sessions)
    with sessions.begin() as session:
        agent = session.get(Agent, agent_id)
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        incarnation.gateway_token_hash = hashlib.sha256(token.encode()).hexdigest()
        incarnation.expires_at = datetime.now(UTC) + timedelta(days=1)
        incarnation_id = incarnation.id
    assert validate_token(token)
    assert not validate_token(token, require_run=True)
    with sessions.begin() as session:
        run = Run(
            agent_id=agent_id,
            incarnation_id=incarnation_id,
            message="test",
            idempotency_key="test",
            request_hash="hash",
            status="dispatching",
            model_id="deepseek/deepseek-v4-flash-0731",
        )
        session.add(run)
        session.flush()
        run_id = run.id
    assert validate_token(token, require_run=True)
    assert selected_request(token)["model_id"] == "deepseek/deepseek-v4-flash-0731"
    assert validate_token(token, require_run=True, run_id=str(run_id))
    assert not validate_token(token, require_run=True, run_id=str(uuid4()))
    report = {"outcome": "length", "reasoning_tokens": 1500, "cost": 0.01}
    record_inference(admit_inference(token, str(run_id)), report)
    with sessions() as session:
        assert session.get(Run, run_id).inference_calls[0].items() >= report.items()
    with sessions.begin() as session:
        session.get(Run, run_id).cancel_requested = True
    assert not validate_token(token, require_run=True)


def test_agent_overrides_inherit_reset_and_snapshot_independently(
    client, sessions, agent_id, monkeypatch
):
    from backend.app.models import InferenceConfig

    async def catalog():
        return [
            {
                "id": name,
                "name": name,
                "context_length": 64000,
                "max_completion_tokens": 8000,
                "supported_parameters": ["temperature"],
                "reasoning": {},
            }
            for name in ("lab/shared", "lab/custom", "lab/next")
        ]

    monkeypatch.setattr("backend.app.inference.catalog", catalog)
    with sessions.begin() as session:
        agent = session.get(Agent, agent_id)
        session.get(WorkloadIncarnation, agent.current_incarnation_id).model_route = "default"
        other = Agent(display_name="Another agent", employee_label="Test")
        session.add(other)
        session.flush()
        other_id = other.id
    path = f"/api/v1/inference/agents/{agent_id}"
    assert client.put("/api/v1/inference", json={"model_id": "lab/shared"}).status_code == 200
    assert client.get(path).json()["inherited"] is True
    assert client.get(path).json()["model_id"] == "lab/shared"
    assert (
        client.put(
            path, json={"model_id": "lab/custom", "settings": {"reasoning_effort": "high"}}
        ).status_code
        == 400
    )
    custom = {"model_id": "lab/custom", "settings": {"temperature": 0.6}}
    assert client.put(path, json=custom).json()["inherited"] is False
    assert client.get(f"/api/v1/inference/agents/{other_id}").json()["model_id"] == "lab/shared"
    run = submit(client, agent_id).json()
    assert run["model_id"] == "lab/custom"
    assert run["inference"]["settings"] == {"temperature": 0.6}
    assert run["inference"]["source"] == "agent"
    assert client.put("/api/v1/inference", json={"model_id": "lab/next"}).status_code == 200
    assert client.get(path).json()["model_id"] == "lab/custom"
    reset = client.delete(path).json()
    assert reset["inherited"] is True and reset["model_id"] == "lab/next"
    assert client.get(f"/api/v1/runs/{run['id']}").json()["model_id"] == "lab/custom"
    with sessions.begin() as session:
        session.get(Run, UUID(run["id"])).status = "completed"
    inherited = submit(client, agent_id, key="inherited").json()
    assert inherited["model_id"] == "lab/next" and inherited["inference"]["source"] == "workspace"
    missing = f"/api/v1/inference/agents/{uuid4()}"
    assert client.get(missing).status_code == 404
    assert client.put(missing, json={"model_id": "fixture"}).status_code == 404
    assert client.delete(missing).status_code == 404
    with sessions.begin() as session:
        config = session.get(InferenceConfig, 1)
        config.model_id, config.settings, config.capabilities = "fixture", {}, {}


@pytest.mark.parametrize("stopping", [False, True])
def test_native_error_is_actionable_with_partial_output(client, sessions, agent_id, stopping):
    run_id = UUID(submit(client, agent_id).json()["id"])
    manager = DiagnosticManager(sessions, AsyncMock())
    manager._claim(run_id)
    manager._event(run_id, {"state": "delta", "deltaText": "Partial response"})
    if stopping:
        with sessions.begin() as session:
            session.get(Agent, agent_id).desired_state = "stopped"
    assert manager._event(
        run_id,
        {
            "state": "error",
            "errorMessage": "401 Unauthorized token=private-secret https://private",
        },
    )
    result = client.get(f"/api/v1/runs/{run_id}").json()
    assert result["output"] == "Partial response"
    assert result["status"] == ("interrupted" if stopping else "failed")
    assert ("agent was stopped" if stopping else "Check the selected provider") in result["error"]
    events = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert "private-secret" not in str(result) + str(events)
    assert "https://private" not in str(result) + str(events)
    assert events[-1]["payload"]["reason"] == result["error"]


@pytest.mark.parametrize("terminal", ["final", "error", "aborted"])
def test_tool_progress_is_durable_bounded_and_run_scoped(client, sessions, agent_id, terminal):
    run_id = submit(client, agent_id).json()["id"]

    class ToolDriver(FakeDriver):
        async def send(self, *args):
            result = await super().send(*args)
            self.event({"state": "tool", "name": "foreign", "phase": "started", "runId": "other"})
            for name, phase in (
                (None, "started"),
                ("secret\ntext", "started"),
                ("x" * 129, "started"),
                ("exec", []),
            ):
                self.event({"state": "tool", "name": name, "phase": phase})
            for name, phase, call in (
                ("exec", "started", "a"),
                ("browser", "started", "b"),
                ("browser", "failed", "b"),
                ("exec", "completed", "a"),
            ):
                self.event(
                    {
                        "state": "tool",
                        "name": name,
                        "phase": phase,
                        "callId": call,
                        "args": "synthetic-secret",
                        "result": "synthetic-secret",
                    }
                )
            self.event({"state": "delta", "deltaText": "Preserved text"})
            self.event({"state": terminal})
            return result

    driver = ToolDriver(streaming=True)
    manager = manager_for(sessions, driver)
    asyncio.run(drain(manager))
    path = f"/api/v1/runs/{run_id}"
    result = client.get(path).json()
    assert (
        result["status"]
        == {"final": "completed", "error": "failed", "aborted": "cancelled"}[terminal]
    )
    assert result["output"] == "Preserved text"
    events = client.get(path + "/events").json()
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    tools = [e["payload"] for e in events if e["type"] == "tool"]
    assert tools == [
        {"name": name, "phase": phase, "callId": call}
        for name, phase, call in (
            ("exec", "started", "a"),
            ("browser", "started", "b"),
            ("browser", "failed", "b"),
            ("exec", "completed", "a"),
        )
    ]
    manager._event(UUID(run_id), {"state": "tool", "name": "late", "phase": "started"})
    assert client.get(path + "/events").json() == events
    assert client.get(path + "/events?after=3").json() == events[3:]
    assert "synthetic-secret" not in str(events)


def ledger_identity(sessions, agent_id, monkeypatch):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from backend.app.models import Employee, Role

    monkeypatch.setattr("gateway.identity.session_factory", lambda: sessions)
    token = "ledger-test-token-at-least-twenty-chars"
    with sessions.begin() as session:
        role = Role(name=f"role-{uuid4()}")
        session.add(role)
        session.flush()
        employee = Employee(name="Ledger owner", role_id=role.id)
        session.add(employee)
        session.flush()
        agent = session.get(Agent, agent_id)
        agent.employee_id = employee.id
        agent.runtime_mode = "native"
        agent.inference_override = {"model_id": "test/model"}
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        incarnation.gateway_token_hash = hashlib.sha256(token.encode()).hexdigest()
        incarnation.expires_at = datetime.now(UTC) + timedelta(days=1)
        return token, employee.id


def test_ledger_durable_idempotent_and_assignment_snapshot(sessions, agent_id, monkeypatch):
    from decimal import Decimal

    from sqlalchemy.exc import IntegrityError

    from backend.app.models import Employee, InferenceCall
    from gateway.identity import admit_inference, record_inference

    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    first = admit_inference(token)
    second = admit_inference(token)
    unresolved = admit_inference(token)
    record_inference(first, {"cost": Decimal("0.100000000001"), "outcome": "cancelled"})
    record_inference(first, {"cost": 99, "outcome": "completed"})
    record_inference(second, {"outcome": "failed"})
    with sessions.begin() as session:
        session.get(Agent, agent_id).employee_id = None
        session.get(Agent, agent_id).observed_state = "deleted"
    with sessions() as session:
        rows = {c.id: c for c in session.scalars(select(InferenceCall))}
        assert len(rows) == 3
        assert rows[first].employee_id == owner
        assert rows[first].cost_usd == Decimal("0.100000000001")
        assert rows[first].outcome == "cancelled"
        assert rows[second].cost_usd is None and rows[second].completed_at is not None
        assert rows[unresolved].cost_usd is None and rows[unresolved].completed_at is None
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.delete(session.get(Employee, owner))


def test_ledger_managed_calls_project_alongside_legacy(client, sessions, agent_id, monkeypatch):
    from gateway.identity import admit_inference, record_inference

    token, _ = ledger_identity(sessions, agent_id, monkeypatch)
    response = submit(client, agent_id)
    run_id = UUID(response.json()["id"])
    with sessions.begin() as session:
        run = session.get(Run, run_id)
        run.status = "running"
        run.legacy_inference_calls = [{"cost": 0.5, "outcome": "completed"}]
    first = admit_inference(token, str(run_id))
    second = admit_inference(token, str(run_id))
    record_inference(first, {"cost": 0, "outcome": "completed"})
    record_inference(second, {"cost": 0.25, "outcome": "completed"})
    calls = client.get(f"/api/v1/runs/{run_id}").json()["inference_calls"]
    assert len(calls) == 3
    assert sum(call["cost"] for call in calls) == 0.75


@pytest.mark.parametrize("runtime", ["openclaw", "hermes"])
def test_native_gateway_accounts_without_chat_run(sessions, agent_id, monkeypatch, runtime):
    import httpx

    from backend.app.models import InferenceCall
    from gateway import main

    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    with sessions.begin() as session:
        session.get(Agent, agent_id).runtime_kind = runtime
    monkeypatch.setattr(main, "provider_key", lambda: "synthetic")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"id": "native-generation", "choices": [], "usage": {"cost": 0.125}}
                )
            ),
            **kw,
        ),
    )
    with TestClient(main.app) as client:
        response = client.post(
            "/native/v1/chat/completions",
            json={"messages": []},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    with sessions() as session:
        call = session.scalar(select(InferenceCall))
        assert call.run_id is None and call.employee_id == owner
        assert float(call.cost_usd) == 0.125 and call.outcome == "completed"


def test_usage_reporting_filters_history_and_pagination(client, sessions, agent_id, monkeypatch):
    from datetime import UTC, datetime
    from decimal import Decimal

    from backend.app.models import InferenceCall
    from gateway.identity import admit_inference, record_inference

    token, owner = ledger_identity(sessions, agent_id, monkeypatch)
    ids = [admit_inference(token) for _ in range(4)]
    record_inference(ids[0], {"cost": Decimal("0.100000000001"), "input_tokens": 4})
    record_inference(ids[1], {"cost": Decimal("0.200000000002"), "output_tokens": 8})
    record_inference(ids[2], {"outcome": "cancelled"})
    with sessions.begin() as session:
        for call in session.scalars(select(InferenceCall)):
            call.admitted_at = datetime(2026, 8, 31, 23, 59, tzinfo=UTC)
        session.get(Agent, agent_id).employee_id = None
        session.get(Agent, agent_id).observed_state = "deleted"
    filters = f"month=2026-08&employee_id={owner}&agent_id={agent_id}"
    data = client.get(f"/api/v1/usage?{filters}").json()
    assert data["total"]["known_spend_usd"] == "0.300000000003"
    assert data["total"]["calls"] == 4 and data["total"]["reported_cost_calls"] == 2
    assert data["total"]["unresolved_calls"] == 1 and data["total"]["missing_cost_calls"] == 1
    assert data["total"]["input_tokens"] == 4 and data["total"]["output_tokens"] == 8
    assert data["employees"][0]["id"] == str(owner)
    assert data["options"]["agents"][0]["deleted"]
    assert data["history_status"] == "before_tracking"
    assert client.get(f"/api/v1/usage?{filters}&month=2026-09").json()["total"]["calls"] == 0
    assert (
        client.get(f"/api/v1/usage?month=2026-08&employee_id={uuid4()}").json()["total"]["calls"]
        == 0
    )
    seen, cursor = [], ""
    for _ in range(4):
        page = client.get(f"/api/v1/usage/calls?{filters}&limit=1&cursor={cursor}").json()
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
    assert set(seen) == {str(value) for value in ids} and len(seen) == 4 and cursor is None
    assert client.get("/api/v1/usage/calls?cursor=bad").status_code == 400
    assert client.get("/api/v1/usage?month=0000-01").status_code == 422
    assert client.get("/api/v1/usage?month=9999-12").status_code == 422
    assert client.get("/api/v1/usage/calls?limit=0").status_code == 422


@pytest.mark.parametrize("expires_during_wait", [False, True])
def test_ledger_admission_uses_time_after_run_lock(
    client, sessions, agent_id, monkeypatch, expires_during_wait
):
    from datetime import UTC, datetime, timedelta
    from threading import Event

    from fastapi import HTTPException
    from sqlalchemy import event

    from backend.app.models import InferenceCall
    from gateway import identity

    token, _ = ledger_identity(sessions, agent_id, monkeypatch)
    run_id = UUID(submit(client, agent_id).json()["id"])
    before = datetime(2030, 9, 30, 23, 59, 59, tzinfo=UTC)
    after = datetime(2030, 10, 1, tzinfo=UTC)
    current = [before]

    class Clock:
        @staticmethod
        def now(_):
            return current[0]

    with sessions.begin() as session:
        run = session.get(Run, run_id)
        run.status = "running"
        incarnation = session.get(WorkloadIncarnation, run.incarnation_id)
        incarnation.expires_at = after if expires_during_wait else after + timedelta(days=1)
    monkeypatch.setattr(identity, "datetime", Clock)
    waiting = Event()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if "runs" in statement and "FOR UPDATE" in statement:
            waiting.set()

    engine = sessions.kw["bind"]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with sessions.begin() as blocker:
                blocker.get(Run, run_id, with_for_update=True)
                event.listen(engine, "before_cursor_execute", before_execute)
                admitted = pool.submit(identity.admit_inference, token, str(run_id))
                assert waiting.wait(timeout=5)
                current[0] = after
            if expires_during_wait:
                with pytest.raises(HTTPException, match="expired"):
                    admitted.result(timeout=5)
            else:
                call_id = admitted.result(timeout=5)
                with sessions() as session:
                    assert session.get(InferenceCall, call_id).admitted_at == after
    finally:
        event.remove(engine, "before_cursor_execute", before_execute)


@pytest.mark.parametrize(
    "fields",
    [["2026-09-01T00:00:00+00:00", value] for value in (123, {}, [], True, None)]
    + [[], {}, "ab", ["2026-09-01T00:00:00+00:00"], [123, str(uuid4())]],
)
def test_usage_rejects_malformed_cursor_fields(client, fields):
    import base64
    import json

    cursor = base64.urlsafe_b64encode(json.dumps(fields).encode()).decode()
    response = client.get("/api/v1/usage/calls", params={"cursor": cursor})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid usage cursor"}
