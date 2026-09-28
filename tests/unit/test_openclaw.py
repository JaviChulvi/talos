import asyncio
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from gateway.fake_model import model_router
from worker.lifecycle import Worker
from worker.openclaw import (
    DeliveryUncertain,
    DeviceIdentity,
    GatewayError,
    OpenClawClient,
)


def test_device_key_persists_with_private_permissions(tmp_path):
    path = tmp_path / "worker.pem"
    first = DeviceIdentity.load_or_create(path)
    assert DeviceIdentity.load_or_create(path).device_id == first.device_id
    assert path.stat().st_mode & 0o777 == 0o600


def test_periodic_runtime_probe_does_not_retry_boot_readiness(monkeypatch, tmp_path):
    settings = SimpleNamespace(worker_state_dir=tmp_path, readiness_timeout_seconds=120)
    monkeypatch.setattr("worker.lifecycle.get_settings", lambda: settings)
    connection = SimpleNamespace(connect=AsyncMock(side_effect=OSError("Wedged runtime")))
    connect = Mock(return_value=connection)
    monkeypatch.setattr("worker.lifecycle.OpenClawClient", connect)
    worker = Worker(sessions=Mock(), client=Mock())
    container = SimpleNamespace(name="runtime", status="running", reload=Mock())
    with pytest.raises(OSError, match="Wedged runtime"):
        asyncio.run(worker.wait_ready(container, "control-token", retry=False))
    assert connect.call_args.kwargs["timeout"] == 2
    connection.connect.assert_awaited_once()


def test_fake_model_rejects_inactive_identity_and_unsupported_model():
    tokens = {"agent-one"}
    app = FastAPI()
    app.include_router(model_router(lambda token: token in tokens))
    with TestClient(app) as client:
        payload = {"model": "fixture", "messages": [{"role": "user", "content": "hello"}]}
        assert client.post("/v1/chat/completions", json=payload).status_code == 401
        headers = {"Authorization": "Bearer agent-one"}
        response = client.post(
            "/v1/chat/completions", json={**payload, "stream": True}, headers=headers
        )
        assert response.status_code == 200
        assert "Talos diagnostic: local fixture response received." in response.text
        assert response.text.endswith("data: [DONE]\n\n")
        assert (
            client.post(
                "/v1/chat/completions", json={**payload, "model": "other"}, headers=headers
            ).status_code
            == 400
        )
        tokens.clear()
        assert client.post("/v1/chat/completions", json=payload, headers=headers).status_code == 401


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("blocked_validation", [1, 2])
def test_gateway_health_remains_responsive_during_identity_lookup(stream, blocked_validation):
    entered, release = Event(), Event()
    validations = 0

    def validate(_):
        nonlocal validations
        validations += 1
        if validations == blocked_validation:
            entered.set()
            assert release.wait(timeout=2), "Identity lookup blocked the event loop"
        return True

    app = FastAPI()
    app.include_router(model_router(validate))

    @app.get("/health/live")
    async def health():
        return {"status": "ok"}

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json={"model": "fixture", "stream": stream},
                    headers={"Authorization": "Bearer active-agent"},
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert (await client.get("/health/live")).status_code == 200
                assert not request.done()
            finally:
                release.set()
                response = await request
            assert response.status_code == 200
            assert validations == 2

    asyncio.run(scenario())


def test_send_disconnection_is_uncertain_and_never_replayed():
    async def scenario():
        received = []

        async def handler(ws):
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "fixture"}}
                )
            )
            connect = json.loads(await ws.recv())
            await ws.send(
                json.dumps(
                    {"type": "res", "id": connect["id"], "ok": True, "payload": {"protocol": 4}}
                )
            )
            received.append(json.loads(await ws.recv()))
            await ws.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with OpenClawClient(
                f"ws://127.0.0.1:{port}", "fixture", DeviceIdentity(Ed25519PrivateKey.generate())
            ) as client:
                with pytest.raises(DeliveryUncertain):
                    await client.send("agent:main:diagnostic", "hello", "one")
        assert len(received) == 1

    asyncio.run(scenario())


def test_gateway_permission_error_is_not_delivery_uncertainty():
    async def scenario():
        async def handler(ws):
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "fixture"}}
                )
            )
            connect = json.loads(await ws.recv())
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": connect["id"],
                        "ok": False,
                        "error": {"code": "NOT_PAIRED", "message": "pairing required"},
                    }
                )
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(GatewayError, match="pairing required"):
                await OpenClawClient(
                    f"ws://127.0.0.1:{port}",
                    "fixture",
                    DeviceIdentity(Ed25519PrivateKey.generate()),
                ).connect()

    asyncio.run(scenario())


def test_history_accepts_valid_large_runtime_responses():
    async def scenario():
        # Below the runtime's 6 MiB history budget and 128 KiB per-message limit.
        messages = [{"role": "assistant", "content": "x" * (120 * 1024)} for _ in range(18)]

        async def handler(ws):
            await ws.send(
                json.dumps(
                    {"type": "event", "event": "connect.challenge", "payload": {"nonce": "fixture"}}
                )
            )
            connect = json.loads(await ws.recv())
            await ws.send(
                json.dumps(
                    {"type": "res", "id": connect["id"], "ok": True, "payload": {"protocol": 4}}
                )
            )
            request = json.loads(await ws.recv())
            assert request["method"] == "chat.history"
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": request["id"],
                        "ok": True,
                        "payload": {"messages": messages},
                    }
                )
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with OpenClawClient(
                f"ws://127.0.0.1:{port}", "fixture", DeviceIdentity(Ed25519PrivateKey.generate())
            ) as client:
                assert (await client.history("agent:main:diagnostic"))["messages"] == messages

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "detail, expected",
    [
        (
            "Another Gateway owner lease is still active for this state directory token=SECRET",
            "lease",
        ),
        ("401 Unauthorized api_key=SECRET", "API key"),
        ("insufficient credits SECRET", "balance"),
        ("429 rate limit exceeded SECRET", "Wait"),
        ("No endpoints found for SECRET", "model"),
        ("getaddrinfo EAI_AGAIN private.SECRET", "egress"),
        ("Chrome not found at /SECRET", "Rebuild"),
        ("tool requires approval for SECRET", "approval"),
        ("unknown failure containing SECRET", None),
        ({"token": "SECRET"}, None),
    ],
)
def test_native_error_classification_never_echoes_details(detail, expected):
    from worker.runtime import runtime_error_message

    message = runtime_error_message(detail)
    if expected is None:
        assert message is None
    else:
        assert expected in message
        assert "SECRET" not in message
