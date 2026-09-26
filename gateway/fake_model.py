"""Authenticated model routing, retaining the deterministic offline fixture."""

import asyncio
import json
import time
import uuid
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from gateway.openrouter import completion as openrouter_completion


def model_router(
    validate_token: Callable[[str], bool], selected_model: Callable[[str], str] | None = None
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions")
    async def completion(request: Request):
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not token
            or not await asyncio.to_thread(validate_token, token)
        ):
            raise HTTPException(401, "Invalid or inactive agent identity")
        try:
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 262144:
                    raise HTTPException(413, "Model request exceeds the size limit")
            body = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "Expected JSON request body") from exc
        if not isinstance(body, dict) or body.get("model") not in ("fixture", "default"):
            raise HTTPException(400, "Only the Talos default or fixture model is supported")
        if body["model"] == "default":
            if selected_model is None:
                raise HTTPException(503, "Model selection is unavailable")
            try:
                model = await asyncio.to_thread(selected_model, token)
            except Exception:
                raise HTTPException(503, "No admitted model request is available") from None
            if model != "fixture":
                return await openrouter_completion(request, body, model, validate_token, token)
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            raise HTTPException(400, "messages must be a list")
        last = next(
            (
                m.get("content", "")
                for m in reversed(messages)
                if isinstance(m, dict) and m.get("role") == "user"
            ),
            "",
        )
        if isinstance(last, list):
            last = " ".join(str(p.get("text", "")) for p in last if isinstance(p, dict))
        if not isinstance(last, str):
            raise HTTPException(400, "Expected text input")
        text = "Talos diagnostic: local fixture response received."
        delay = 15 if "[slow]" in last else 0
        response_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        base = {"id": response_id, "created": created, "model": "fixture"}
        if not body.get("stream"):
            if delay:
                await asyncio.sleep(delay)
            if not await asyncio.to_thread(validate_token, token):
                raise HTTPException(401, "Invalid or inactive agent identity")
            return {
                **base,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

        async def chunks():
            def event(delta, finish=None):
                payload = {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish,
                        }
                    ],
                }
                return "data: " + json.dumps(payload) + "\n\n"

            yield event({"role": "assistant", "content": ""})
            if delay:
                await asyncio.sleep(delay)
            # Check again before returning content; revocation also affects slow calls.
            if not await asyncio.to_thread(validate_token, token):
                return
            yield event({"content": text})
            yield event({}, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return router
