"""Конфигурация приложения (читается из .env через pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/photoslibrary.appendonly "
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"
)


class Settings(BaseSettings):
    """Все настройки проекта. Имена полей совпадают с ключами в .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    bot_token: str
    admin_id: int
    admin_username: str = ""

    # --- Mini App ---
    webapp_url: str
    host: str = "127.0.0.1"
    port: int = 8000

    # --- Google Photos ---
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_scopes: str = DEFAULT_SCOPES

    # --- Поведение ---
    album_debounce_seconds: float = 1.5
    album_debounce_max_wait: float = 15.0
    session_timeout_seconds: int = 900
    initdata_max_age_seconds: int = 86400
    success_notice_seconds: int = 3
    db_path: str = "whitelist.db"

    @field_validator("webapp_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def admin_mention(self) -> str:
        """ADMIN_USERNAME без ведущей '@' — для подстановки в текст отказа."""
        return self.admin_username.lstrip("@")

    @property
    def database_path(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else BASE_DIR / path

    @property
    def google_ready(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    def webapp_link(self, session_id: str) -> str:
        return f"{self.webapp_url}?session_id={session_id}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
