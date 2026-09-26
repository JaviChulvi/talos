from fastapi import FastAPI

app = FastAPI(title="Talos gateway", docs_url=None, redoc_url=None)


@app.get("/health/live")
def live():
    return {"status": "ok", "mode": "scaffold"}
