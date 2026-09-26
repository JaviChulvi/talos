from fastapi import FastAPI

from gateway.fake_model import model_router
from gateway.identity import selected_model, validate_token

app = FastAPI(title="Talos gateway", docs_url=None, redoc_url=None)
app.include_router(
    model_router(lambda token: validate_token(token, require_run=True), selected_model)
)


@app.get("/health/live")
def live():
    return {"status": "ok", "mode": "talos-model-gateway"}
