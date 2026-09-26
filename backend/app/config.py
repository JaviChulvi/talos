from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TALOS_")

    database_url: str = "postgresql+psycopg://talos:talos-local-development@localhost:5432/talos"
    static_dir: Path = Path("frontend/dist")
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
