"""Bounded text-only OpenRouter transport. Never forward caller credentials or options."""

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

MAX_TOKENS = 1024
MAX_OUTPUT = 16000
REQUEST_TIMEOUT = 120
CHECK_INTERVAL = 1.0


class InferenceError(Exception):
    pass


def text_messages(body: dict) -> list[dict]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise HTTPException(400, "Expected 1 to 256 text messages")
    result = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in (
            "system",
            "developer",
            "user",
            "assistant",
        ):
            raise HTTPException(400, "Only text conversation roles are supported")
        content = message.get("content")
        if isinstance(content, list):
            if not all(
                isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
                for p in content
            ):
                raise HTTPException(400, "Only text content is supported")
            content = "\n".join(p["text"] for p in content)
        if (
            not isinstance(content, str)
            or message.get("tool_calls")
            or message.get("function_call")
        ):
            raise HTTPException(400, "Only text content is supported")
        result.append({"role": message["role"], "content": content})
    return result


async def upstream_chunks(client: httpx.AsyncClient, key: str, model: str, messages: list[dict]):
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": MAX_TOKENS,
        "reasoning": {"enabled": False},
    }
    async with client.stream(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
    ) as response:
        if response.status_code != 200:
            raise InferenceError(
                "OpenRouter rejected the request; check gateway credentials, "
                "credits and model availability"
            )
        if "text/event-stream" not in response.headers.get("content-type", ""):
            raise InferenceError("OpenRouter returned an invalid stream")
        data = []
        pending = ""
        output_size = 0
        finish = None
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
                if sum(map(len, data)) > 131072:
                    raise InferenceError("OpenRouter response frame exceeded the limit")
            if line or not data:
                continue
            value = "\n".join(data)
            data.clear()
            if value == "[DONE]":
                if finish is None or output_size == 0:
                    raise InferenceError("OpenRouter ended without a complete text response")
                if pending:
                    yield {"content": pending}, None
                yield {}, finish
                return
            try:
                chunk = json.loads(value)
                if not isinstance(chunk, dict) or chunk.get("error"):
                    raise InferenceError("OpenRouter generation failed")
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        raise InferenceError("OpenRouter returned multiple choices")
                    delta = choice.get("delta", {})
                    if delta.get("tool_calls") or delta.get("function_call"):
                        raise InferenceError("Tool calls are disabled")
                    content = delta.get("content") or ""
                    if not isinstance(content, str):
                        raise InferenceError("OpenRouter returned invalid text")
                    output_size += len(content)
                    if output_size > MAX_OUTPUT:
                        raise InferenceError("OpenRouter output exceeded the character limit")
                    pending += content
                    if len(pending) >= 64:
                        yield {"content": pending}, None
                        pending = ""
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if reason not in {"stop", "length"}:
                            raise InferenceError(
                                "OpenRouter generation did not complete successfully"
                            )
                        finish = reason
            except (ValueError, TypeError, AttributeError):
                raise InferenceError("OpenRouter returned an invalid response") from None
        raise InferenceError("OpenRouter stream ended unexpectedly")


async def guarded_chunks(request, client, key, model, messages, validate, token, streaming):
    queue = asyncio.Queue(maxsize=1)

    async def produce():
        try:
            async with aclosing(upstream_chunks(client, key, model, messages)) as chunks:
                async for chunk in chunks:
                    await queue.put(chunk)
        except (InferenceError, httpx.HTTPError) as error:
            safe_error = (
                error
                if isinstance(error, InferenceError)
                else InferenceError("OpenRouter connection failed")
            )
            await queue.put(safe_error)
        finally:
            # Cancellation must never block trying to enqueue a sentinel.
            if not asyncio.current_task().cancelling():
                await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            next_check = 0
            while True:
                if time.monotonic() >= next_check:
                    if not await asyncio.to_thread(validate, token):
                        raise InferenceError("Agent access was revoked or expired")
                    if not streaming and await request.is_disconnected():
                        raise InferenceError("Caller disconnected")
                    next_check = time.monotonic() + CHECK_INTERVAL
                try:
                    item = await asyncio.wait_for(
                        queue.get(), max(0.001, next_check - time.monotonic())
                    )
                except TimeoutError:
                    continue
                if isinstance(item, Exception):
                    raise item
                if item is None:
                    return
                yield item
    except TimeoutError:
        raise InferenceError("OpenRouter request timed out") from None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def completion(request: Request, body: dict, model: str, validate: Callable, token: str):
    messages = text_messages(body)
    streaming = body.get("stream", False)
    if not isinstance(streaming, bool):
        raise HTTPException(400, "stream must be a boolean")
    try:
        key = (
            Path(os.environ.get("TALOS_OPENROUTER_KEY_FILE", "/run/secrets/openrouter_api_key"))
            .read_text()
            .strip()
        )
        if not key or any(c.isspace() for c in key):
            raise ValueError
    except (OSError, ValueError):
        raise HTTPException(
            503, "OpenRouter is not configured; mount the gateway API key secret"
        ) from None
    base = {"id": "chatcmpl-" + uuid.uuid4().hex, "created": int(time.time()), "model": model}

    async def chunks():
        # No automatic retries: a disconnected paid request may already have run.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10), trust_env=False
        ) as client:
            async with aclosing(
                guarded_chunks(request, client, key, model, messages, validate, token, streaming)
            ) as guarded:
                async for delta, finish in guarded:
                    yield {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }

    if streaming:

        async def events():
            try:
                async with aclosing(chunks()) as stream:
                    async for chunk in stream:
                        yield "data: " + json.dumps(chunk) + "\n\n"
                yield "data: [DONE]\n\n"
            except InferenceError as error:
                yield (
                    "data: "
                    + json.dumps({"error": {"message": str(error), "type": "inference_error"}})
                    + "\n\n"
                )

        return StreamingResponse(events(), media_type="text/event-stream")

    output = ""
    finish = None
    try:
        async with aclosing(chunks()) as stream:
            async for chunk in stream:
                choice = chunk["choices"][0]
                output += choice["delta"].get("content", "")
                finish = choice["finish_reason"] or finish
    except InferenceError as error:
        raise HTTPException(502, str(error)) from None
    return {
        **base,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output},
                "finish_reason": finish,
            }
        ],
    }
