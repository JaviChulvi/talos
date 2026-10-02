from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from backend.app.runtime_versions import DEFAULT_RUNTIME_VERSIONS, validate_catalog


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TALOS_")

    database_url: str = "postgresql+psycopg://talos:talos-local-development@localhost:5432/talos"
    database_password: str | None = None
    static_dir: Path = Path("frontend/dist")
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]
    allowed_origins: list[str] = ["http://127.0.0.1:8000", "http://localhost:8000"]
    admin_cookie_secure: bool = True
    openrouter_app_key_file: Path = Path("/var/lib/talos-provider/openrouter.key")
    setup_artifacts_dir: Path = Path("/var/lib/talos-setups")
    connection_secrets_dir: Path = Path("/var/lib/talos-connections")
    worker_state_dir: Path = Path("/var/lib/talos")
    installation_id: str = Field(default="local", pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    compose_project: str = Field(default="talos", pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    worker_container: str | None = None
    runtime_versions: dict[str, dict[str, str]] = DEFAULT_RUNTIME_VERSIONS
    lifecycle_poll_seconds: float = Field(default=1, ge=0.1, le=60)
    readiness_timeout_seconds: int = Field(default=120, ge=10, le=600)

    # Operational resource limits, independent of model generation settings.
    inference_timeout_seconds: int = Field(default=1800, ge=30)
    inference_idle_timeout_seconds: int = Field(default=300, ge=30)
    inference_max_output_chars: int = Field(default=4_000_000, ge=16000)
    inference_max_request_bytes: int = Field(default=16_777_216, ge=262144)

    _validate_runtime_versions = field_validator("runtime_versions")(validate_catalog)

    @property
    def connection_url(self) -> URL:
        url = make_url(self.database_url)
        return url if self.database_password is None else url.set(password=self.database_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
