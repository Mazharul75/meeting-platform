"""Settings loaded from environment variables only. No secrets live in code."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MIN_SECRET_LEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "prod", "test"] = "dev"
    app_base_url: str = "http://localhost:8000"
    secret_key: str = ""
    master_key: str = ""

    database_url: str = ""
    # Session-pooler URL used only by Alembic. Falls back to database_url.
    migration_database_url: str = ""

    supabase_url: str = ""
    supabase_service_key: str = ""
    storage_bucket: str = "recordings"
    # supabase | local | memory. Empty = supabase when configured, else local (dev only).
    storage_backend: str = ""

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    sentry_dsn: str = ""

    # Recording engine settings handed to the browser.
    part_seconds: int = 240
    timeslice_ms: int = 5000
    max_part_bytes: int = 35 * 1024 * 1024
    recording_retention_days: int = 90
    storage_quota_bytes: int = 1024 * 1024 * 1024

    upload_url_ttl_seconds: int = 600
    download_url_ttl_seconds: int = 600

    session_idle_hours: int = 8
    session_absolute_days: int = 7
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    login_rate_per_minute: int = 10

    default_timezone: str = "Asia/Dhaka"
    enable_dev_tools: bool = False

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def cookie_secure(self) -> bool:
        return self.is_prod

    @property
    def dev_tools_enabled(self) -> bool:
        return self.app_env in ("dev", "test") or self.enable_dev_tools

    @property
    def resolved_storage_backend(self) -> str:
        if self.storage_backend:
            return self.storage_backend
        if self.supabase_url and self.supabase_service_key:
            return "supabase"
        return "local"

    @property
    def supabase_origin(self) -> str:
        return _origin(self.supabase_url)

    @property
    def livekit_configured(self) -> bool:
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)

    @property
    def livekit_origins(self) -> list[str]:
        if not self.livekit_url:
            return []
        parsed = urlparse(self.livekit_url)
        host = parsed.netloc
        if not host:
            return []
        return [f"wss://{host}", f"https://{host}"] if parsed.scheme in ("wss", "https") else [
            f"ws://{host}",
            f"http://{host}",
        ]

    @model_validator(mode="after")
    def _check_prod(self) -> Settings:
        if self.app_env != "prod":
            return self
        missing: list[str] = []
        if len(self.secret_key) < _MIN_SECRET_LEN:
            missing.append("SECRET_KEY (min 32 chars)")
        if len(self.master_key) < _MIN_SECRET_LEN:
            missing.append("MASTER_KEY (min 32 chars)")
        if not self.database_url:
            missing.append("DATABASE_URL")
        if self.resolved_storage_backend != "supabase":
            missing.append("SUPABASE_URL and SUPABASE_SERVICE_KEY")
        if not self.app_base_url.startswith("https://"):
            missing.append("APP_BASE_URL (must start with https:// in prod)")
        if missing:
            raise ValueError("Missing or invalid settings in prod: " + ", ".join(missing))
        return self


def _origin(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
