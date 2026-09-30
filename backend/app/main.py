from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.app.administration import router as administration_router
from backend.app.agents import router as agents_router
from backend.app.auth import require_admin
from backend.app.auth import router as auth_router
from backend.app.availability import platform_status
from backend.app.availability import router as availability_router
from backend.app.channels import router as channels_router
from backend.app.config import get_settings
from backend.app.connections import router as connections_router
from backend.app.db import Database, get_engine
from backend.app.diagnostics import router as diagnostics_router
from backend.app.handoff import router as handoff_router
from backend.app.inference import router as inference_router
from backend.app.readiness import router as readiness_router
from backend.app.setups import router as setups_router
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
    app = FastAPI(title="Talos", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    management = APIRouter(dependencies=[Depends(require_admin)])

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        return JSONResponse({"detail": "Service unavailable"}, status_code=503)

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
    async def browser_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api/") and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            if origin is not None and origin not in settings.allowed_origins:
                return JSONResponse({"detail": "Untrusted browser origin"}, status_code=403)
            if request.headers.get("x-talos-request") != "1":
                return JSONResponse({"detail": "Expected Talos request header"}, status_code=403)
            if (
                request.url.path == "/api/v1/auth/login"
                and request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return JSONResponse({"detail": "Expected JSON request"}, status_code=415)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        ok = database_ready()
        return JSONResponse(
            {"status": "ready" if ok else "unavailable"}, status_code=200 if ok else 503
        )

    @management.get("/api/v1/status")
    def status(session: Database):
        ok = database_ready()
        try:
            services = platform_status(session) if ok else {}
        except SQLAlchemyError:
            services = {}
        worker = services.get("worker", {}).get("state", "unknown")
        gateway = services.get("gateway", {}).get("state", "unknown")
        return {
            "status": "ok" if ok and worker == gateway == "ok" else "degraded",
            "database": "ready" if ok else "unavailable",
            "worker": worker,
            "gateway": gateway,
            "services": services,
            "version": "0.1.0",
        }

    management.include_router(administration_router)
    management.include_router(handoff_router)
    management.include_router(agents_router)
    management.include_router(availability_router)
    management.include_router(connections_router)
    management.include_router(channels_router)
    management.include_router(diagnostics_router)
    management.include_router(inference_router)
    management.include_router(readiness_router)
    management.include_router(setups_router)
    management.include_router(usage_router)
    app.include_router(auth_router)
    app.include_router(management)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def missing_api(path: str):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")
    return app


app = create_app()
