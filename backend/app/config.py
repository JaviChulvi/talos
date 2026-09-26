from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TALOS_")

    database_url: str = "postgresql+psycopg://talos:talos-local-development@localhost:5432/talos"
    database_password: str | None = None
    static_dir: Path = Path("frontend/dist")
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]

    @property
    def connection_url(self) -> URL:
        url = make_url(self.database_url)
        return url if self.database_password is None else url.set(password=self.database_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()
