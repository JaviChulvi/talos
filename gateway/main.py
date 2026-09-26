from fastapi import FastAPI

from gateway.fake_model import model_router

app = FastAPI(title="Talos gateway", docs_url=None, redoc_url=None)
# Production defaults closed until database-backed workload identity is installed.
app.include_router(model_router(lambda _: False))


@app.get("/health/live")
def live():
    return {"status": "ok", "mode": "scaffold"}
