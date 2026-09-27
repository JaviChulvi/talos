from fastapi import FastAPI

from gateway.fake_model import model_router
from gateway.identity import record_inference, selected_request, validate_token

app = FastAPI(title="Talos gateway", docs_url=None, redoc_url=None)
app.include_router(
    model_router(
        lambda token: validate_token(token, require_run=True),
        selected_request,
        validate_run=lambda token, run_id: validate_token(token, require_run=True, run_id=run_id),
        record_usage=record_inference,
    )
)


@app.get("/health/live")
def live():
    return {"status": "ok", "mode": "talos-model-gateway"}
