"""Local operator model selection; provider credentials stay in the gateway."""

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app.agents import Database
from backend.app.models import InferenceConfig

router = APIRouter(prefix="/api/v1/inference")
RECOMMENDED_MODEL = "deepseek/deepseek-v4-flash-0731"


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=255)


async def catalog() -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            models = response.json()["data"]
        return sorted(
            [
                {"id": model["id"], "name": model["name"]}
                for model in models
                if "text" in model.get("architecture", {}).get("input_modalities", [])
                and model.get("architecture", {}).get("output_modalities") == ["text"]
                and model.get("context_length", 0) >= 32000
                and "max_tokens" in model.get("supported_parameters", [])
                and not model["id"].startswith(("~", "openrouter/"))
                and not any(suffix in model["id"] for suffix in (":online", ":batch"))
            ],
            key=lambda model: (model["id"] != RECOMMENDED_MODEL, model["name"]),
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(
            503, "OpenRouter model catalog is unavailable; selection is unchanged"
        ) from None


@router.get("")
def selection(session: Database):
    return {"model_id": session.get(InferenceConfig, 1).model_id}


@router.get("/models")
async def models():
    return {"models": await catalog(), "recommended_model": RECOMMENDED_MODEL}


@router.put("")
async def update_selection(body: ModelSelection, session: Database):
    if body.model_id != "fixture" and body.model_id not in {m["id"] for m in await catalog()}:
        raise HTTPException(400, "Choose a supported text model from the OpenRouter catalog")
    with session.begin():
        config = session.get(InferenceConfig, 1, with_for_update=True)
        config.model_id = body.model_id
    return {"model_id": body.model_id}
