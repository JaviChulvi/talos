import asyncio
import json
from contextlib import aclosing

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.app.config import get_settings
from gateway.fake_model import model_router
from gateway.identity import (
    admit_inference,
    native_selection,
    record_inference,
    selected_request,
    validate_token,
)
from gateway.openrouter import provider_key, provider_key_source

app = FastAPI(title="Talos gateway", docs_url=None, redoc_url=None)
app.include_router(
    model_router(
        lambda token: validate_token(token, require_run=True),
        selected_request,
        validate_run=lambda token, run_id: validate_token(token, require_run=True, run_id=run_id),
        record_usage=record_inference,
        admit_usage=admit_inference,
    )
)


@app.get("/health/live")
def live():
    return {"status": "ok", "mode": "talos-model-gateway"}


@app.get("/health/provider")
def provider_status():
    try:
        provider_key()
        return {"configured": True, "source": provider_key_source()}
    except HTTPException:
        return {"configured": False, "source": provider_key_source()}


@app.post("/native/v1/chat/completions")
async def native_completion(request: Request):
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    selection = (
        await asyncio.to_thread(native_selection, token) if scheme.lower() == "bearer" else None
    )
    if not selection:
        raise HTTPException(401, "Native OpenRouter access is inactive")
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > get_settings().inference_max_request_bytes:
            raise HTTPException(413, "Model request exceeds the size limit")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Expected JSON request body") from None
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise HTTPException(400, "Expected native conversation messages")
    # Native runtimes own tool schemas, dispatch and generation settings. The gateway
    # owns credentials, destination and model choice; never forward caller headers.
    payload = {
        key: body[key]
        for key in (
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "reasoning",
            "reasoning_effort",
            "stop",
            "stream",
            "stream_options",
        )
        if key in body
    }
    payload["model"] = selection["model_id"]
    if not isinstance(payload.get("stream", False), bool):
        raise HTTPException(400, "stream must be a boolean")
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(get_settings().inference_idle_timeout_seconds, connect=10),
        trust_env=False,
    )
    pending_headers = None
    try:
        pending_headers = asyncio.create_task(
            client.send(
                client.build_request(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {provider_key()}"},
                    json=payload,
                ),
                stream=True,
            )
        )
        async with asyncio.timeout(get_settings().inference_timeout_seconds):
            while True:
                if not await asyncio.to_thread(validate_token, token):
                    raise HTTPException(401, "Native OpenRouter access was revoked")
                if await request.is_disconnected():
                    raise HTTPException(499, "Caller disconnected")
                done, _ = await asyncio.wait({pending_headers}, timeout=1)
                if done:
                    upstream = pending_headers.result()
                    break
    except BaseException:
        if pending_headers is not None:
            pending_headers.cancel()
            await asyncio.gather(pending_headers, return_exceptions=True)
        await client.aclose()
        raise
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(
            502,
            "OpenRouter rejected the request; check credentials, credits and model availability",
        )

    async def chunks():
        size = 0
        iterator = upstream.aiter_bytes().__aiter__()
        pending = None
        try:
            async with asyncio.timeout(get_settings().inference_timeout_seconds):
                while True:
                    if not await asyncio.to_thread(validate_token, token):
                        return
                    if not payload.get("stream") and await request.is_disconnected():
                        return
                    if pending is None:
                        pending = asyncio.create_task(anext(iterator))
                    done, _ = await asyncio.wait({pending}, timeout=1)
                    if not done:
                        continue
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        break
                    pending = None
                    size += len(chunk)
                    if size > get_settings().inference_max_output_chars * 6:
                        return
                    yield chunk
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await upstream.aclose()
            await client.aclose()

    if payload.get("stream"):
        return StreamingResponse(chunks(), media_type="text/event-stream")
    async with aclosing(chunks()) as stream:
        result = b"".join([part async for part in stream])
    try:
        return json.loads(result)
    except ValueError:
        raise HTTPException(502, "OpenRouter returned an incomplete response") from None
