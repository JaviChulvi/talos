import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import openrouter
from gateway.fake_model import model_router

MODEL = "deepseek/deepseek-v4-flash-0731"
HEADERS = {"Authorization": "Bearer talos-agent"}
BODY = {"model": "default", "messages": [{"role": "user", "content": "hello"}]}


def frame(content="", finish=None):
    return (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": finish,
                    }
                ]
            }
        )
        + "\n\n"
    )


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("provider-secret")
    monkeypatch.setenv("TALOS_OPENROUTER_KEY_FILE", str(key))
    calls = []
    state = SimpleNamespace(
        status=200,
        data=frame("Hello from upstream", "stop") + "data: [DONE]\n\n",
    )

    async def handler(request):
        calls.append(request)
        return httpx.Response(
            state.status, text=state.data, headers={"content-type": "text/event-stream"}
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        openrouter.httpx,
        "AsyncClient",
        lambda **kw: client_class(
            transport=httpx.MockTransport(handler),
            **kw,
        ),
    )
    app = FastAPI()
    app.include_router(model_router(lambda token: token == "talos-agent", lambda _: MODEL))
    with TestClient(app) as client:
        yield client, calls, state


@pytest.mark.parametrize("stream", [False, True])
def test_routes_only_admitted_model_and_never_forwards_caller_credentials(gateway, stream):
    client, calls, _ = gateway
    response = client.post(
        "/v1/chat/completions",
        headers=HEADERS,
        json={
            **BODY,
            "stream": stream,
            "max_tokens": 999999,
            "api_key": "caller-key",
            "provider": {"order": ["attacker"]},
            "models": ["other/model"],
            "base_url": "https://attacker.example",
            "tools": [{"type": "function"}],
        },
    )
    assert response.status_code == 200
    assert "Hello from upstream" in response.text
    assert "provider-secret" not in response.text
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer provider-secret"
    payload = json.loads(request.content)
    assert payload == {
        "model": MODEL,
        "messages": BODY["messages"],
        "stream": True,
        "max_tokens": 1024,
        "reasoning": {"enabled": False},
    }
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
    else:
        assert response.json()["choices"][0]["finish_reason"] == "stop"


def test_denies_identity_models_images_and_oversized_input_before_upstream(gateway):
    client, calls, _ = gateway
    assert client.post("/v1/chat/completions", json=BODY).status_code == 401
    for bad in (
        {**BODY, "model": "another/model"},
        {**BODY, "messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
    ):
        assert client.post("/v1/chat/completions", headers=HEADERS, json=bad).status_code == 400
    large = {**BODY, "messages": [{"role": "user", "content": "x" * 262145}]}
    assert client.post("/v1/chat/completions", headers=HEADERS, json=large).status_code == 413
    assert calls == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["status", "midstream", "truncated", "malformed", "oversize"])
def test_provider_failures_are_sanitized_and_never_finish_successfully(gateway, stream, failure):
    client, calls, state = gateway
    if failure == "status":
        state.status, state.data = 401, "provider-secret"
    elif failure == "midstream":
        state.data = frame("partial " * 10) + 'data: {"error":{"message":"provider-secret"}}\n\n'
    elif failure == "truncated":
        state.data = frame("partial", "stop")
    elif failure == "malformed":
        state.data = "data: {broken provider-secret\n\n"
    else:
        state.data = frame("x" * 16001, "stop") + "data: [DONE]\n\n"
    response = client.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": stream})
    assert "provider-secret" not in response.text
    assert "[DONE]" not in response.text
    assert "error" in response.text if stream else response.status_code == 502
    assert len(calls) == 1  # No replay of potentially paid requests.


def test_missing_key_does_not_fall_back_to_fixture(gateway, monkeypatch):
    client, calls, _ = gateway
    monkeypatch.setenv("TALOS_OPENROUTER_KEY_FILE", "/missing/talos-key")
    response = client.post("/v1/chat/completions", headers=HEADERS, json=BODY)
    assert response.status_code == 503
    assert "fixture response" not in response.text
    assert calls == []


@pytest.mark.parametrize("reason", ["revoke", "timeout", "cancel", "disconnect"])
def test_silent_upstream_is_closed_on_revocation_timeout_or_disconnect(monkeypatch, reason):
    monkeypatch.setattr(openrouter, "CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(openrouter, "REQUEST_TIMEOUT", 0.08)

    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        active = True

        async def silent(*args):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {}, None
            finally:
                closed.set()

        monkeypatch.setattr(openrouter, "upstream_chunks", silent)

        async def disconnected():
            return reason == "disconnect"

        request = SimpleNamespace(is_disconnected=disconnected)

        async def consume():
            async with aclosing(
                openrouter.guarded_chunks(
                    request, None, "key", MODEL, [], lambda _: active, "token", False
                )
            ) as chunks:
                async for _ in chunks:
                    pass

        task = asyncio.create_task(consume())
        if reason != "disconnect":
            await asyncio.wait_for(entered.wait(), 1)
        if reason == "revoke":
            active = False
        if reason == "cancel":
            task.cancel()
        expected = asyncio.CancelledError if reason == "cancel" else openrouter.InferenceError
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
        assert closed.is_set() or not entered.is_set()

    asyncio.run(scenario())
