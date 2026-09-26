from fastapi import FastAPI

from gateway.fake_model import model_router
from gateway.identity import validate_token

app = FastAPI(title="Talos gateway", docs_url=None, redoc_url=None)
app.include_router(model_router(validate_token))


@app.get("/health/live")
def live():
    return {"status": "ok", "mode": "diagnostic-fixture"}
