import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.services.ai import build_provider
from app.services.ai.gemini_provider import GeminiProvider
from app.services.ai.openai_provider import OpenAIProvider


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/chatbot",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_gemini_settings_default_to_the_free_tier_provider() -> None:
    settings = _settings(
        gemini_api_key=SecretStr("unit-test-gemini-key"),
        gemini_model="gemini-3.8-flash",
    )

    assert settings.ai_provider == "gemini"
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.gemini_timeout_seconds == 60
    assert settings.gemini_api_key.get_secret_value() == "unit-test-gemini-key"
    assert "unit-test-gemini-key" not in repr(settings)


def test_build_provider_follows_ai_provider() -> None:
    gemini = build_provider(_settings(ai_provider="gemini"))
    openai = build_provider(_settings(ai_provider="openai"))

    assert isinstance(gemini, GeminiProvider)
    assert isinstance(openai, OpenAIProvider)


def test_ai_provider_rejects_an_unknown_name() -> None:
    with pytest.raises(ValidationError):
        _settings(ai_provider="claude")
