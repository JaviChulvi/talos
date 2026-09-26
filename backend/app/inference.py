"""Server-owned model capabilities and optional generation settings."""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app.agents import Database
from backend.app.models import InferenceConfig

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


@router.put("")
async def update_selection(body: ModelSelection, session: Database):
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
    with session.begin():
        config = session.get(InferenceConfig, 1, with_for_update=True)
        config.model_id, config.settings, config.capabilities = (
            body.model_id,
            settings,
            capabilities,
        )
    return config_response(config)
