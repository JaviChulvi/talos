"""Server-owned model capabilities and optional generation settings."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.agents import (
    Database,
    IdempotencyKey,
    OperationResponse,
    enqueue_operation,
    find_replay,
    recover_duplicate,
    request_hash,
)
from backend.app.models import ACTIVE_OPERATION_STATUSES, Agent, InferenceConfig, Operation

router = APIRouter(prefix="/api/v1/inference")
RECOMMENDED_MODEL = "deepseek/deepseek-v4-flash-0731"


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=20)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=255)
    settings: GenerationSettings = Field(default_factory=GenerationSettings)


async def catalog() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            models = response.json()["data"]
        result = []
        for model in models:
            architecture = model.get("architecture") or {}
            if (
                "text" not in architecture.get("input_modalities", [])
                or architecture.get("output_modalities") != ["text"]
                or model["id"].startswith(("~", "openrouter/"))
                or any(suffix in model["id"] for suffix in (":online", ":batch"))
            ):
                continue
            provider = model.get("top_provider") or {}
            context = min(
                size
                for size in (model.get("context_length"), provider.get("context_length"))
                if isinstance(size, int) and size > 0
            )
            result.append(
                {
                    "id": model["id"],
                    "name": model["name"],
                    "context_length": context,
                    "max_completion_tokens": provider.get("max_completion_tokens"),
                    "supported_parameters": model.get("supported_parameters") or [],
                    "reasoning": model.get("reasoning") or {},
                    "pricing": {
                        key: (model.get("pricing") or {}).get(key)
                        for key in ("prompt", "completion")
                    },
                }
            )
        return sorted(result, key=lambda m: (m["id"] != RECOMMENDED_MODEL, m["name"]))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(
            503, "OpenRouter model catalog is unavailable; selection is unchanged"
        ) from None


def validate_settings(settings: dict, model: dict):
    supported = model.get("supported_parameters", [])
    for key in ("temperature", "top_p"):
        if key in settings and key not in supported:
            raise HTTPException(400, f"This model does not support {key}")
    if "max_output_tokens" in settings:
        if not {"max_tokens", "max_completion_tokens"}.intersection(supported):
            raise HTTPException(400, "This model does not support an output-token override")
        limit = model.get("max_completion_tokens") or model["context_length"]
        if settings["max_output_tokens"] > min(limit, model["context_length"]):
            raise HTTPException(400, "Output-token limit exceeds this model's capacity")
    effort = settings.get("reasoning_effort")
    if effort is not None:
        reasoning = model.get("reasoning") or {}
        if not reasoning or not {"reasoning", "reasoning_effort"}.intersection(supported):
            raise HTTPException(400, "This model does not support reasoning overrides")
        if effort == "none" and reasoning.get("mandatory"):
            raise HTTPException(400, "Reasoning cannot be disabled for this model")
        efforts = reasoning.get("supported_efforts", [])
        if effort not in (
            efforts
            if efforts is not None
            else ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
        ):
            if effort != "none" or reasoning.get("mandatory"):
                raise HTTPException(400, "Choose a supported reasoning effort")


def config_response(config):
    return {
        "model_id": config.model_id,
        "settings": config.settings,
        "capabilities": config.capabilities,
    }


@router.get("")
def selection(session: Database):
    return config_response(session.get(InferenceConfig, 1))


@router.get("/models")
async def models():
    return {"models": await catalog(), "recommended_model": RECOMMENDED_MODEL}


async def validated_selection(body: ModelSelection) -> dict:
    settings = body.settings.model_dump(exclude_none=True)
    capabilities = {}
    if body.model_id == "fixture":
        if settings:
            raise HTTPException(400, "The local simulator has no generation settings")
    else:
        capabilities = next((m for m in await catalog() if m["id"] == body.model_id), None)
        if capabilities is None:
            raise HTTPException(400, "Choose a supported text model from the OpenRouter catalog")
        validate_settings(settings, capabilities)
    return {"model_id": body.model_id, "settings": settings, "capabilities": capabilities}


@router.put("")
async def update_selection(body: ModelSelection, session: Database):
    selection = await validated_selection(body)
    with session.begin():
        config = session.get(InferenceConfig, 1, with_for_update=True)
        config.model_id = selection["model_id"]
        config.settings = selection["settings"]
        config.capabilities = selection["capabilities"]
    return config_response(config)


def agent_config(session, agent_id, *, lock=False):
    agent = session.get(Agent, agent_id, with_for_update=lock)
    if agent is None or agent.desired_state == "deleted" or agent.observed_state == "deleted":
        raise HTTPException(404, "Agent not found")
    return agent


def agent_config_response(session, agent):
    return {
        **(
            agent.inference_override
            or (
                {"model_id": None, "settings": {}, "capabilities": {}}
                if agent.runtime_mode == "native"
                else config_response(session.get(InferenceConfig, 1))
            )
        ),
        "inherited": agent.inference_override is None,
    }


@router.get("/agents/{agent_id}")
def agent_selection(agent_id: UUID, session: Database):
    return agent_config_response(session, agent_config(session, agent_id))


@router.put("/agents/{agent_id}")
async def update_agent_selection(agent_id: UUID, body: ModelSelection, session: Database):
    selection = await validated_selection(body)
    with session.begin():
        agent = agent_config(session, agent_id, lock=True)
        if agent.runtime_mode == "native":
            raise HTTPException(409, "Use the native model operation for this agent")
        agent.inference_override = selection
        return agent_config_response(session, agent)


@router.delete("/agents/{agent_id}")
def reset_agent_selection(agent_id: UUID, session: Database):
    with session.begin():
        agent = agent_config(session, agent_id, lock=True)
        if agent.runtime_mode == "native":
            raise HTTPException(409, "Use the native model operation for this agent")
        agent.inference_override = None
        return agent_config_response(session, agent)


@router.get("/provider")
async def provider_status():
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get("http://gateway:8001/health/provider")
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        return {"configured": False}


@router.post("/agents/{agent_id}/native", status_code=202, response_model=OperationResponse)
async def update_native_selection(
    agent_id: UUID,
    idempotency_key: IdempotencyKey,
    session: Database,
    body: Annotated[ModelSelection | None, Body()] = None,
):
    scope = f"agent:{agent_id}:configure_model"
    digest = request_hash(body.model_dump() if body else {})
    replay = find_replay(session, scope, idempotency_key, digest)
    session.rollback()
    if replay:
        return replay
    selection = await validated_selection(body) if body else {}
    if body and (body.model_id == "fixture" or selection["settings"]):
        raise HTTPException(
            400, "Choose an OpenRouter model; native generation settings stay in the runtime"
        )
    try:
        with session.begin():
            agent = agent_config(session, agent_id, lock=True)
            replay = find_replay(session, scope, idempotency_key, digest)
            if replay:
                return replay
            if agent.runtime_mode != "native":
                raise HTTPException(409, "This agent uses managed model settings")
            if session.scalar(
                select(Operation.id).where(
                    Operation.agent_id == agent_id,
                    Operation.status.in_(ACTIVE_OPERATION_STATUSES),
                )
            ):
                raise HTTPException(409, "Wait for the current agent operation to finish")
            operation = enqueue_operation(
                session,
                agent,
                "configure_model",
                scope,
                idempotency_key,
                digest,
            )
            operation.model_selection = selection
            return operation
    except IntegrityError as error:
        return recover_duplicate(session, scope, idempotency_key, digest, error)
