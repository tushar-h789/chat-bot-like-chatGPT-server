from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration loaded from the environment and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str
    cors_origins: str = "http://localhost:3000"
    session_secret: SecretStr = SecretStr("")
    ai_provider: Literal["gemini", "openai"] = "gemini"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""
    openai_timeout_seconds: float = Field(default=60, gt=0, le=300)
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    gemini_timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_message_chars: int = Field(default=16000, ge=1, le=100_000)
    max_body_bytes: int = Field(default=6_291_456, ge=1, le=20_000_000)
    max_upload_bytes: int = Field(default=5_242_880, ge=1, le=20_000_000)
    upload_dir: Path = BACKEND_ROOT / "var" / "uploads"
    rate_limit_auth_per_minute: int = Field(default=20, ge=0, le=10_000)
    rate_limit_chat_per_minute: int = Field(default=30, ge=0, le=10_000)
    usage_limit_tokens_per_day: int = Field(default=100_000, ge=0, le=100_000_000)
    admin_emails: str = ""
    gemini_input_usd_per_million: float | None = None
    gemini_output_usd_per_million: float | None = None
    openai_input_usd_per_million: float | None = None
    openai_output_usd_per_million: float | None = None

    @computed_field
    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]

    @computed_field
    @property
    def admin_email_set(self) -> frozenset[str]:
        return frozenset(
            email.strip().lower()
            for email in self.admin_emails.split(",")
            if email.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
