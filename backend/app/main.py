from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.app.administration import router as administration_router
from backend.app.agents import router as agents_router
from backend.app.config import get_settings
from backend.app.db import get_engine
from backend.app.diagnostics import router as diagnostics_router
from backend.app.inference import router as inference_router
from backend.app.usage import router as usage_router


def database_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            current = MigrationContext.configure(connection).get_current_revision()
        expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        return current == expected and current is not None
    except SQLAlchemyError:
        return False


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Talos", version="0.1.0")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Validation failures must not echo submitted credentials, including model-level errors.
        return JSONResponse(
            {
                "detail": [
                    {key: value for key, value in item.items() if key not in {"input", "ctx"}}
                    for item in error.errors()
                ]
            },
            status_code=422,
        )

    @app.middleware("http")
    async def local_browser_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlsplit

            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
                "localhost",
                "127.0.0.1",
            }:
                return JSONResponse({"detail": "Untrusted browser origin"}, status_code=403)
        return await call_next(request)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        ok = database_ready()
        return JSONResponse(
            {"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503
        )

    @app.get("/api/v1/status")
    def status():
        ok = database_ready()
        return {
            "status": "ok" if ok else "degraded",
            "database": "ready" if ok else "unavailable",
            "worker": "configured",
            "gateway": "configured",
            "version": "0.1.0",
        }

    app.include_router(administration_router)
    app.include_router(agents_router)
    app.include_router(diagnostics_router)
    app.include_router(inference_router)
    app.include_router(usage_router)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def missing_api(path: str):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")
    return app


app = create_app()
