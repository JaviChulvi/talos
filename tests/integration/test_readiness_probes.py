"""Explicit checks never dispatch native chat or publish evidence from an old revision."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from backend.app.availability import heartbeat, record_check
from backend.app.models import (
    Agent,
    ChannelOutbox,
    ChannelProbe,
    InferenceConfig,
    Run,
    ServiceHeartbeat,
    UserChannel,
    WorkloadIncarnation,
)
from connector.delivery import TransportError
from connector.main import Connector

# ruff: noqa: F401, F811
from tests.integration.test_channel_runs import (
    channel_setup,
    client,
    database_engine,
    lifecycle_sessions,
    profile_agent,
    ready_accesses,
    session_maker,
    worker,
)
from worker.diagnostics import DiagnosticManager
from worker.readiness import Readiness

pytestmark = pytest.mark.integration


def request_probe(client, agent_id, kind="model", key="probe-test"):
    return client.post(
        f"/api/v1/agents/{agent_id}/checks", json={"kind": kind}, headers={"Idempotency-Key": key}
    )


def execute_probe(worker, identifier):
    connector = AsyncMock()
    manager = DiagnosticManager(worker.sessions, connector, probe=Readiness(worker).execute)
    asyncio.run(manager._execute(UUID(identifier)))
    connector.assert_not_called()


def test_probe_admission_is_shared_and_idempotent(client, ready_accesses):
    agent_id, _ = ready_accesses
    first = request_probe(client, agent_id)
    assert first.status_code == 202 and first.json()["source"] == "probe"
    assert request_probe(client, agent_id).json()["id"] == first.json()["id"]
    assert request_probe(client, agent_id, kind="runtime", key="another").status_code == 409
    assert (
        client.post(
            f"/api/v1/agents/{agent_id}/diagnostic-runs",
            json={"message": "Hello"},
            headers={"Idempotency-Key": "admin"},
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/agents/{agent_id}/runs").json() == []


def test_unsupported_native_probe_does_not_send_model_or_native_chat(
    client, ready_accesses, worker, monkeypatch
):
    agent_id, _ = ready_accesses
    transport = AsyncMock()
    monkeypatch.setattr("worker.readiness.httpx.AsyncClient", transport)
    run = request_probe(client, agent_id).json()
    execute_probe(worker, run["id"])
    transport.assert_not_called()
    check = next(
        row
        for row in client.get(f"/api/v1/agents/{agent_id}/availability").json()["checks"]
        if row["kind"] == "model"
    )
    assert check["state"] == "unknown" and check["code"] == "native_safe_probe_unavailable"
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "completed"


@pytest.mark.parametrize(
    "status, expected", [(200, "ok"), (401, "blocked"), (402, "blocked"), (429, "blocked")]
)
def test_safe_model_probe_uses_actual_gateway_route_without_tools(
    client, session_maker, ready_accesses, worker, monkeypatch, status, expected
):
    agent_id, _ = ready_accesses
    with session_maker.begin() as session:
        session.get(Agent, UUID(agent_id)).inference_override = {"model_id": "lab/model"}
    calls = []

    def handle(request):
        import json

        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/native/v1/chat/completions"
        assert body["model"] == "lab/model" and body["max_tokens"] == 16
        assert body["stream"] is False and "tools" not in body
        return httpx.Response(status, json={"choices": [{"message": {"content": "OK"}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.readiness.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(
        "worker.readiness.read_credentials",
        lambda _: {"agent_token": "synthetic", "control_token": "synthetic"},
    )
    run = request_probe(client, agent_id).json()
    execute_probe(worker, run["id"])
    assert len(calls) == 1
    check = next(
        row
        for row in client.get(f"/api/v1/agents/{agent_id}/availability").json()["checks"]
        if row["kind"] == "model"
    )
    assert check["state"] == expected


def test_old_model_result_is_discarded_after_configuration_change(
    client, session_maker, ready_accesses, worker, monkeypatch
):
    agent_id, _ = ready_accesses
    with session_maker.begin() as session:
        session.get(Agent, UUID(agent_id)).inference_override = {"model_id": "lab/model"}

    def handle(_):
        with session_maker.begin() as session:
            session.get(Agent, UUID(agent_id)).revision += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.readiness.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr("worker.readiness.read_credentials", lambda _: {"agent_token": "synthetic"})
    run = request_probe(client, agent_id).json()
    execute_probe(worker, run["id"])
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "interrupted"
    check = next(
        row
        for row in client.get(f"/api/v1/agents/{agent_id}/availability").json()["checks"]
        if row["kind"] == "model"
    )
    assert check["state"] == "stale"


def test_cancelled_probe_does_not_publish_success(
    client, session_maker, ready_accesses, worker, monkeypatch
):
    agent_id, _ = ready_accesses
    with session_maker.begin() as session:
        session.get(Agent, UUID(agent_id)).inference_override = {"model_id": "lab/model"}
    run = request_probe(client, agent_id).json()

    def handle(_):
        client.post(f"/api/v1/runs/{run['id']}/cancel")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.readiness.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr("worker.readiness.read_credentials", lambda _: {"agent_token": "synthetic"})
    execute_probe(worker, run["id"])
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "cancelled"


def test_disabled_channel_can_verify_credentials_without_polling_or_sending(
    client, session_maker, ready_accesses, monkeypatch
):
    _, pairs = ready_accesses
    channel = pairs[0][1]
    client.put(f"/api/v1/channels/{channel['id']}", json={"name": "Staff", "enabled": False})
    path = f"/api/v1/channels/{channel['id']}/check"
    first = client.post(path, headers={"Idempotency-Key": "check-channel"})
    assert first.status_code == 202
    assert (
        client.post(path, headers={"Idempotency-Key": "check-channel"}).json()["id"]
        == first.json()["id"]
    )
    assert client.post(path, headers={"Idempotency-Key": "another"}).status_code == 409
    transport = AsyncMock()
    transport.verify.return_value = {"bot_id": "42", "username": "test_bot"}
    monkeypatch.setattr("connector.main.Telegram", lambda _: transport)
    asyncio.run(Connector(session_maker).process_check())
    transport.poll.assert_not_awaited()
    transport.send.assert_not_awaited()
    assert (
        client.get(f"/api/v1/channel-checks/{first.json()['id']}").json()["status"] == "completed"
    )
    assert (
        client.get(f"/api/v1/channels/{channel['id']}/availability").json()["state"]
        == "not_applicable"
    )


def test_channel_check_failure_is_safe_and_rotation_discards_queued_check(
    client, session_maker, ready_accesses, monkeypatch
):
    _, pairs = ready_accesses
    channel = pairs[0][1]
    path = f"/api/v1/channels/{channel['id']}/check"
    probe = client.post(path, headers={"Idempotency-Key": "broken"}).json()
    transport = AsyncMock()
    transport.verify.side_effect = TransportError("invalid_credentials")
    monkeypatch.setattr("connector.main.Telegram", lambda _: transport)
    asyncio.run(Connector(session_maker).process_check())
    assert (
        client.get(f"/api/v1/channel-checks/{probe['id']}").json()["code"] == "invalid_credentials"
    )
    stale = client.post(path, headers={"Idempotency-Key": "stale"}).json()
    client.put(
        f"/api/v1/channels/{channel['id']}/credentials", json={"values": {"bot_token": "new"}}
    )
    asyncio.run(Connector(session_maker).process_check())
    assert client.get(f"/api/v1/channel-checks/{stale['id']}").json()["status"] == "stale"
    assert transport.verify.await_count == 1


def test_channel_failure_does_not_hide_other_channel(client, session_maker, ready_accesses):
    agent_id, pairs = ready_accesses
    owner = Connector(session_maker)
    for _, channel in pairs:
        assert owner.report(
            UUID(channel["id"]),
            channel["revision"],
            UUID(channel["credential_version_id"]),
            "ok" if channel["provider"] == "telegram" else "blocked",
            "polling_active" if channel["provider"] == "telegram" else "invalid_credentials",
        )
    with session_maker.begin() as session:
        heartbeat(session, "worker")
        heartbeat(session, "connector")
        record_check(session, session.get(Agent, UUID(agent_id)), "model", "ok", "model_responded")
    result = client.get(f"/api/v1/agents/{agent_id}/availability").json()
    accesses = {row["provider"]: row for row in result["accesses"]}
    assert result["status"] == "available"
    assert accesses["telegram"]["status"] == "available"
    assert accesses["slack"]["status"] == "requires_action"


def test_connection_check_uses_discovery_without_native_chat(
    client, session_maker, ready_accesses, worker, monkeypatch
):
    from unittest.mock import Mock

    agent_id, _ = ready_accesses
    with session_maker.begin() as session:
        agent = session.get(Agent, UUID(agent_id))
        agent.applied_application = {
            **agent.applied_application,
            "setup": {"manifest": {}},
            "connector_grants": ["crm"],
        }
    discovery = Mock(return_value={})
    monkeypatch.setattr("worker.readiness.verify_setup", discovery)
    run = request_probe(client, agent_id, kind="connections").json()
    execute_probe(worker, run["id"])
    assert discovery.call_args.kwargs["discover"] is True
    assert discovery.call_args.kwargs["network"] == worker.names(UUID(agent_id))[1]
    checks = {
        row["kind"]: row
        for row in client.get(f"/api/v1/agents/{agent_id}/availability").json()["checks"]
    }
    assert checks["connections"]["state"] == "ok" and checks["setup"]["state"] == "ok"


def test_runtime_check_uses_owned_readiness_without_inference(
    client, ready_accesses, worker, monkeypatch
):
    from unittest.mock import Mock

    agent_id, _ = ready_accesses
    worker.owned_container = Mock(return_value=object())
    worker.wait_ready = AsyncMock()
    monkeypatch.setattr(
        "worker.readiness.read_credentials", lambda _: {"control_token": "synthetic"}
    )
    run = request_probe(client, agent_id, kind="runtime").json()
    execute_probe(worker, run["id"])
    assert worker.wait_ready.call_args.kwargs == {"runtime_kind": "openclaw", "retry": False}


def test_model_timeout_keeps_uncertain_run_without_retry(
    client, session_maker, ready_accesses, worker, monkeypatch
):
    agent_id, _ = ready_accesses
    with session_maker.begin() as session:
        session.get(Agent, UUID(agent_id)).inference_override = {"model_id": "lab/model"}
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-url-and-token")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.readiness.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr("worker.readiness.read_credentials", lambda _: {"agent_token": "synthetic"})
    run = request_probe(client, agent_id).json()
    execute_probe(worker, run["id"])
    execute_probe(worker, run["id"])
    assert len(calls) == 1
    result = client.get(f"/api/v1/runs/{run['id']}").json()
    assert result["status"] == "unknown" and "private-url" not in str(result)


@pytest.mark.parametrize("change", ["cancel", "configuration"])
def test_uncertain_model_check_keeps_admission_after_concurrent_change(
    client, session_maker, ready_accesses, worker, monkeypatch, change
):
    agent_id, _ = ready_accesses
    with session_maker.begin() as session:
        session.get(Agent, agent_id).inference_override = {"model_id": "lab/model"}
    run = request_probe(client, agent_id).json()

    def handle(request):
        if change == "cancel":
            assert client.post(f"/api/v1/runs/{run['id']}/cancel").status_code == 200
        else:
            with session_maker.begin() as session:
                session.get(Agent, agent_id).revision += 1
        raise httpx.ReadTimeout("Response not confirmed", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.readiness.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    execute_probe(worker, run["id"])
    assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "unknown"
    assert request_probe(client, agent_id, key="next-probe").status_code == 409


@pytest.mark.parametrize("source", ["agent", "workspace"])
@pytest.mark.parametrize("limit", [None, 8, 8192])
def test_managed_probe_uses_selected_model_and_bounded_provider_request(
    client, session_maker, ready_accesses, worker, monkeypatch, source, limit
):
    from gateway.main import app as gateway
    from worker.lifecycle import read_credentials

    agent_id, _ = ready_accesses
    settings = {} if limit is None else {"max_output_tokens": limit}
    selection = {
        "model_id": "lab/model",
        "settings": settings,
        "capabilities": {"id": "lab/model", "supported_parameters": ["max_tokens"]},
    }
    with session_maker.begin() as session:
        agent = session.get(Agent, agent_id)
        agent.runtime_mode = "managed"
        incarnation = session.get(WorkloadIncarnation, agent.current_incarnation_id)
        incarnation.model_route = "default"
        if source == "agent":
            agent.inference_override = selection
        else:
            config = session.get(InferenceConfig, 1)
            config.model_id = selection["model_id"]
            config.settings = settings
            config.capabilities = selection["capabilities"]
        credentials = read_credentials(incarnation)
    calls = []
    local_gateway = httpx.ASGITransport(app=gateway)

    async def handle(request):
        if request.url.host == "talos-gateway":
            assert request.url.path == "/v1/chat/completions"
            assert json.loads(request.content)["model"] == "default"
            return await local_gateway.handle_async_request(request)
        assert request.url.host == "openrouter.ai"
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "lab/model" and "tools" not in body
        event = {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "worker.readiness.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr("gateway.openrouter.provider_key", lambda: "synthetic-provider-key")
    run = request_probe(client, agent_id).json()
    execute_probe(worker, run["id"])
    assert (
        client.get(f"/api/v1/runs/{run['id']}").json()["output"] == "model_responded_without_tools"
    )
    assert len(calls) == 1 and calls[0]["max_tokens"] == min(limit or 16, 16)
    # Ordinary conversations retain their configured limit, independently of the probe.
    normal = client.post(
        f"/api/v1/agents/{agent_id}/diagnostic-runs",
        json={"message": "Normal conversation"},
        headers={"Idempotency-Key": "normal-after-probe"},
    )
    assert normal.status_code == 202
    with session_maker.begin() as session:
        session.get(Run, UUID(normal.json()["id"])).status = "dispatching"

    async def normal_request():
        async with httpx.AsyncClient() as http:
            return await http.post(
                "http://talos-gateway/v1/chat/completions",
                headers={"Authorization": "Bearer " + credentials["agent_token"]},
                json={"model": "default", "messages": [{"role": "user", "content": "Hello"}]},
            )

    assert asyncio.run(normal_request()).status_code == 200
    assert calls[1].get("max_tokens") == limit


def test_slow_channel_check_does_not_block_other_delivery_or_heartbeat(
    client, session_maker, ready_accesses, monkeypatch
):
    _, pairs = ready_accesses
    telegram, slack = pairs[0][1], pairs[1][1]
    probe = client.post(
        f"/api/v1/channels/{slack['id']}/check", headers={"Idempotency-Key": "slow-check"}
    ).json()

    async def scenario():
        checking = asyncio.Event()
        release = asyncio.Event()
        transport = AsyncMock()

        async def verify(_):
            checking.set()
            await release.wait()
            return {"bot_id": "B12345"}

        transport.verify.side_effect = verify
        transport.connected.return_value = True
        monkeypatch.setattr("connector.main.Slack", lambda *_: transport)
        owner = Connector(session_maker)
        owner.delivery.send_one = AsyncMock()
        owner.consume = AsyncMock(side_effect=lambda *_: release.wait())
        # Keep a healthy, already owned consumer while Slack's network check waits.
        consumer = asyncio.create_task(release.wait())
        owner.tasks[UUID(telegram["id"])] = (
            telegram["revision"],
            UUID(telegram["credential_version_id"]),
            consumer,
        )
        healthy = object()
        owner.transports[UUID(telegram["id"])] = healthy
        try:
            await asyncio.wait_for(owner.tick(), timeout=2)
            await asyncio.wait_for(checking.wait(), timeout=2)
            first = client.get(f"/api/v1/channel-checks/{probe['id']}").json()
            assert first["status"] == "running"
            with session_maker() as session:
                previous_heartbeat = session.get(ServiceHeartbeat, "connector").checked_at
            await asyncio.wait_for(owner.tick(), timeout=2)
            assert owner.delivery.send_one.await_count == 2
            owner.delivery.send_one.assert_awaited_with(UUID(telegram["id"]), healthy)
            owner.consume.assert_not_called()
            with session_maker() as session:
                assert session.get(ServiceHeartbeat, "connector").checked_at > previous_heartbeat
            # Shutdown also owns the in-flight check and persists its interrupted state.
        finally:
            await owner.close()
        assert owner.check_task.done() and consumer.done()
        assert not owner.checking
        transport.close.assert_awaited_once()

    asyncio.run(scenario())
    assert client.get(f"/api/v1/channel-checks/{probe['id']}").json()["code"] == "check_interrupted"
