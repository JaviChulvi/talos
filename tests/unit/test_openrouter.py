import asyncio
import json
from contextlib import aclosing
from threading import Event
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


def test_native_revocation_cancels_provider_before_headers(monkeypatch):
    from gateway import main

    state = {"allowed": True, "cancelled": False}
    monkeypatch.setattr(main, "provider_key", lambda: "synthetic-key")
    monkeypatch.setattr(
        main, "native_selection", lambda _: {"model_id": "test/model"} if state["allowed"] else None
    )

    monkeypatch.setattr(main, "validate_token", lambda _: state["allowed"])

    async def upstream(request):
        state["allowed"] = False
        try:
            await asyncio.Event().wait()
        finally:
            state["cancelled"] = True

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    with TestClient(main.app) as client:
        result = client.post("/native/v1/chat/completions", json=BODY, headers=HEADERS)
    assert result.status_code == 401
    assert state["cancelled"]


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("revoke", [False, True])
def test_native_reset_preserves_admitted_request_unless_token_revoked(monkeypatch, phase, revoke):
    from gateway import main

    state = {"selection": {"model_id": "original/model"}, "closed": False}
    checked = Event()
    calls = []

    def valid_token(_):
        if state["selection"] is None:
            checked.set()
            return not revoke
        return True

    monkeypatch.setattr(main, "provider_key", lambda: "synthetic-key")
    monkeypatch.setattr(main, "native_selection", lambda _: state["selection"])
    monkeypatch.setattr(main, "validate_token", valid_token)

    class Reply(httpx.AsyncByteStream):
        async def __aiter__(self):
            if phase == "body":
                state["selection"] = None
            yield b'data: {"choices": []}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            state["closed"] = True

    async def upstream(request):
        calls.append(json.loads(request.content))
        if phase == "headers":
            state["selection"] = None
            assert await asyncio.to_thread(checked.wait, 5)
        return httpx.Response(200, stream=Reply())

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    with TestClient(main.app) as client:
        result = client.post(
            "/native/v1/chat/completions", json={**BODY, "stream": True}, headers=HEADERS
        )
        assert (
            client.post("/native/v1/chat/completions", json=BODY, headers=HEADERS).status_code
            == 401
        )
    assert len(calls) == 1 and calls[0]["model"] == "original/model"
    if revoke and phase == "headers":
        assert result.status_code == 401
    else:
        assert result.status_code == 200
        assert ("[DONE]" in result.text) is not revoke
        assert state["closed"]


@pytest.mark.parametrize("streaming", [True, False])
def test_native_route_preserves_tools_and_owns_credentials_and_model(
    monkeypatch, tmp_path, streaming
):
    from gateway import main

    key = tmp_path / "key"
    key.write_text("shared-provider-secret")
    monkeypatch.setenv("TALOS_OPENROUTER_KEY_FILE", str(key))
    monkeypatch.setattr(
        main,
        "native_selection",
        lambda token: {"model_id": "chosen/model"} if token == "native-agent" else None,
    )
    monkeypatch.setattr(main, "validate_token", lambda token: token == "native-agent")
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "read", "arguments": "{}"},
    }
    reply = {
        "choices": [
            {
                "message": {"role": "assistant", "tool_calls": [tool_call]},
                "finish_reason": "tool_calls",
            }
        ]
    }
    calls = []

    async def upstream(request):
        calls.append(request)
        return httpx.Response(
            200,
            text="data: " + json.dumps(reply) + "\n\ndata: [DONE]\n\n"
            if streaming
            else json.dumps(reply),
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    body = {
        "model": "untrusted/model",
        "stream": streaming,
        "messages": [{"role": "tool", "tool_call_id": "call-0", "content": "file contents"}],
        "tools": [
            {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}
        ],
        "reasoning_effort": "high",
        "api_key": "injected",
        "provider": {"only": ["untrusted"]},
    }
    with TestClient(main.app) as client:
        assert (
            client.post(
                "/native/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer inactive"},
            ).status_code
            == 401
        )
        result = client.post(
            "/native/v1/chat/completions",
            json=body,
            headers={"Authorization": "Bearer native-agent"},
        )
    assert result.status_code == 200
    assert "tool_calls" in result.text
    assert "shared-provider-secret" not in result.text
    forwarded = json.loads(calls[0].content)
    assert forwarded["messages"] == body["messages"] and forwarded["tools"] == body["tools"]
    assert forwarded["model"] == "chosen/model"
    assert forwarded["reasoning_effort"] == "high"
    assert "api_key" not in forwarded and "provider" not in forwarded
    assert calls[0].headers["authorization"] == "Bearer shared-provider-secret"


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
        settings={},
        reports=[],
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
    app.include_router(
        model_router(
            lambda token: token == "talos-agent",
            lambda _: {"model_id": MODEL, "run_id": "run", "settings": state.settings},
            record_usage=lambda _run, report: state.reports.append(report),
        )
    )
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
        "stream_options": {"include_usage": True},
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
    large = {**BODY, "messages": [{"role": "user", "content": "x" * 16_777_217}]}
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
        state.data = frame("x" * (openrouter.MAX_OUTPUT + 1), "stop") + "data: [DONE]\n\n"
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


@pytest.mark.parametrize("stream", [False, True])
def test_explicit_settings_reasoning_usage_and_length_finish(gateway, stream):
    client, calls, state = gateway
    state.settings = {
        "reasoning_effort": "high",
        "max_output_tokens": 8000,
        "temperature": 0.7,
        "top_p": 0.9,
    }
    reasoning = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "reasoning": "Thinking",
                    "reasoning_details": [{"type": "reasoning.text", "text": "Thinking"}],
                },
            }
        ]
    }
    usage = {
        "id": "generation-1",
        "model": MODEL,
        "choices": [],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 8000,
            "total_tokens": 8020,
            "cost": 0.002,
            "completion_tokens_details": {"reasoning_tokens": 8000},
        },
    }
    state.data = (
        "data: "
        + json.dumps(reasoning)
        + "\n\n"
        + frame("", "length")
        + "data: "
        + json.dumps(usage)
        + "\n\ndata: [DONE]\n\n"
    )
    response = client.post("/v1/chat/completions", headers=HEADERS, json={**BODY, "stream": stream})
    assert response.status_code == 200
    payload = json.loads(calls[0].content)
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["max_tokens"] == 8000
    assert payload["temperature"] == 0.7 and payload["top_p"] == 0.9
    assert payload["provider"] == {"require_parameters": True}
    report = state.reports[0]
    assert report["outcome"] == "length" and report["finish_reason"] == "length"
    assert report["input_tokens"] == 20 and report["reasoning_tokens"] == 8000
    assert report["cost"] == 0.002 and report["duration_ms"] >= 0
    assert "Thinking" in response.text
    if not stream:
        assert response.json()["usage"]["total_tokens"] == 8020


def test_missing_usage_is_not_recorded_as_zero(gateway):
    client, _, state = gateway
    assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 200
    assert state.reports[0]["outcome"] == "completed"
    assert "cost" not in state.reports[0] and "input_tokens" not in state.reports[0]


def test_reasoning_counts_towards_operational_output_limit(gateway, monkeypatch):
    client, _, state = gateway
    monkeypatch.setattr(openrouter, "MAX_OUTPUT", 50)
    state.data = "data: " + json.dumps({"choices": [{"delta": {"reasoning": "x" * 51}}]}) + "\n\n"
    assert client.post("/v1/chat/completions", headers=HEADERS, json=BODY).status_code == 502
    assert state.reports[0]["outcome"] == "output_limit"
