import asyncio
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from gateway.fake_model import model_router
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
