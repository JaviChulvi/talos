"""Bounded text-only OpenRouter transport. Never forward caller credentials or options."""

import asyncio
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Callable
from contextlib import aclosing
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.app.config import get_settings

MAX_OUTPUT = get_settings().inference_max_output_chars
REQUEST_TIMEOUT = get_settings().inference_timeout_seconds
CHECK_INTERVAL = 1.0


class InferenceError(Exception):
    def __init__(self, message, outcome="failed"):
        super().__init__(message)
        self.outcome = outcome


def text_messages(body: dict) -> list[dict]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "Expected a non-empty list of text messages")
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
        normalized = {"role": message["role"], "content": content}
        if message["role"] == "assistant":
            if isinstance(message.get("reasoning"), str):
                normalized["reasoning"] = message["reasoning"]
            if isinstance(message.get("reasoning_details"), list):
                normalized["reasoning_details"] = message["reasoning_details"]
        result.append(normalized)
    return result


async def upstream_chunks(
    client: httpx.AsyncClient,
    key: str,
    model: str,
    messages: list[dict],
    settings=None,
    report=None,
):
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    settings = settings or {}
    report = report if report is not None else {}
    for name in ("temperature", "top_p"):
        if name in settings:
            payload[name] = settings[name]
    if "max_output_tokens" in settings:
        payload["max_tokens"] = settings["max_output_tokens"]
    if "reasoning_effort" in settings:
        payload["reasoning"] = {"effort": settings["reasoning_effort"]}
    if settings:
        payload["provider"] = {"require_parameters": True}
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
                if finish is None:
                    raise InferenceError("OpenRouter ended without a complete text response")
                if pending:
                    yield {"content": pending}, None
                yield {}, finish
                return
            try:
                chunk = json.loads(value)
                if not isinstance(chunk, dict) or chunk.get("error"):
                    raise InferenceError("OpenRouter generation failed")
                if isinstance(chunk.get("id"), str):
                    report["generation_id"] = chunk["id"][:200]
                if isinstance(chunk.get("model"), str):
                    report["model"] = chunk["model"][:255]
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                    for source, dest in (
                        ("prompt_tokens", "input_tokens"),
                        ("completion_tokens", "output_tokens"),
                        ("total_tokens", "total_tokens"),
                        ("cost", "cost"),
                    ):
                        number = usage.get(source)
                        if (
                            isinstance(number, (int, float))
                            and math.isfinite(number)
                            and number >= 0
                        ):
                            report[dest] = number
                    details = usage.get("completion_tokens_details") or {}
                    if isinstance(details, dict) and isinstance(
                        details.get("reasoning_tokens"), int
                    ):
                        report["reasoning_tokens"] = details["reasoning_tokens"]
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
                        raise InferenceError(
                            "OpenRouter output exceeded the character limit", "output_limit"
                        )
                    reasoning = {
                        k: delta[k]
                        for k in ("reasoning", "reasoning_content", "reasoning_details")
                        if delta.get(k)
                    }
                    output_size += len(json.dumps(reasoning)) if reasoning else 0
                    if output_size > MAX_OUTPUT:
                        raise InferenceError(
                            "OpenRouter output exceeded the character limit", "output_limit"
                        )
                    if reasoning:
                        yield reasoning, None
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
                        report["finish_reason"] = reason
            except (ValueError, TypeError, AttributeError):
                raise InferenceError("OpenRouter returned an invalid response") from None
        raise InferenceError("OpenRouter stream ended unexpectedly")


async def guarded_chunks(
    request, client, key, model, messages, validate, token, streaming, settings=None, report=None
):
    queue = asyncio.Queue(maxsize=1)

    async def produce():
        try:
            async with aclosing(
                upstream_chunks(client, key, model, messages, settings, report)
            ) as chunks:
                async for chunk in chunks:
                    await queue.put(chunk)
        except Exception as error:
            safe_error = (
                error
                if isinstance(error, InferenceError)
                else InferenceError("OpenRouter connection timed out", "timed_out")
                if isinstance(error, httpx.TimeoutException)
                else InferenceError("OpenRouter connection failed")
            )
            await queue.put(safe_error)
        finally:
            # Cancellation must never block trying to enqueue a sentinel.
            if not asyncio.current_task().cancelling():
                await queue.put(None)

    if not await asyncio.to_thread(validate, token):
        raise InferenceError("Agent access was revoked or expired", "revoked")
    task = asyncio.create_task(produce())
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            next_check = 0
            while True:
                if time.monotonic() >= next_check:
                    if not await asyncio.to_thread(validate, token):
                        raise InferenceError("Agent access was revoked or expired", "revoked")
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
        raise InferenceError("OpenRouter request timed out", "timed_out") from None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def completion(
    request: Request,
    body: dict,
    model: str,
    validate: Callable,
    token: str,
    *,
    settings=None,
    record_usage=None,
):
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
        report = {"model": model}
        started = time.monotonic()
        try:
            async with aclosing(stream_chunks(report)) as upstream:
                async for chunk in upstream:
                    yield chunk
        except BaseException as error:
            report["outcome"] = (
                "cancelled"
                if isinstance(error, (asyncio.CancelledError, GeneratorExit))
                else error.outcome
                if isinstance(error, InferenceError)
                else "failed"
            )
            if isinstance(error, InferenceError):
                report["error"] = str(error)
            raise
        finally:
            report.setdefault(
                "outcome", "length" if report.get("finish_reason") == "length" else "completed"
            )
            report["duration_ms"] = round((time.monotonic() - started) * 1000)
            if record_usage:
                try:
                    await asyncio.shield(asyncio.to_thread(record_usage, report))
                except Exception:
                    logging.error(
                        "Could not persist inference usage; provider usage may be unavailable"
                    )

    async def stream_chunks(report):
        # No automatic retries: a disconnected paid request may already have run.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(get_settings().inference_idle_timeout_seconds, connect=10),
            trust_env=False,
        ) as client:
            async with aclosing(
                guarded_chunks(
                    request,
                    client,
                    key,
                    model,
                    messages,
                    validate,
                    token,
                    streaming,
                    settings,
                    report,
                )
            ) as guarded:
                async for delta, finish in guarded:
                    yield {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                        **(
                            {
                                "usage": {
                                    "prompt_tokens": report.get("input_tokens", 0),
                                    "completion_tokens": report.get("output_tokens", 0),
                                    "total_tokens": report.get("total_tokens", 0),
                                    "completion_tokens_details": {
                                        "reasoning_tokens": report.get("reasoning_tokens", 0)
                                    },
                                }
                            }
                            if finish is not None and "total_tokens" in report
                            else {}
                        ),
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
    reasoning = ""
    reasoning_details = []
    usage = {}
    finish = None
    try:
        async with aclosing(chunks()) as stream:
            async for chunk in stream:
                choice = chunk["choices"][0]
                output += choice["delta"].get("content", "")
                reasoning += choice["delta"].get(
                    "reasoning", choice["delta"].get("reasoning_content", "")
                )
                reasoning_details.extend(choice["delta"].get("reasoning_details", []))
                usage = chunk.get("usage", usage)
                finish = choice["finish_reason"] or finish
    except InferenceError as error:
        raise HTTPException(502, str(error)) from None
    return {
        **base,
        "object": "chat.completion",
        **({"usage": usage} if usage else {}),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": output,
                    **({"reasoning": reasoning} if reasoning else {}),
                    **({"reasoning_details": reasoning_details} if reasoning_details else {}),
                },
                "finish_reason": finish,
            }
        ],
    }
