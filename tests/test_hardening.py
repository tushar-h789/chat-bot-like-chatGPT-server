from decimal import Decimal

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.services.usage import _provider_cost
from tests.test_auth import _csrf


def test_live_health_sets_security_headers() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in response.headers


def test_production_adds_strict_transport_security() -> None:
    settings = get_settings().model_copy(update={"environment": "production"})
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/v1/health/live")

    assert response.headers["strict-transport-security"].startswith("max-age=")


def test_oversized_body_is_rejected() -> None:
    settings = get_settings().model_copy(update={"max_body_bytes": 16})
    with TestClient(create_app(settings=settings)) as client:
        response = client.post(
            "/api/v1/auth/login",
            content=b"x" * 40,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_login_attempts_are_rate_limited() -> None:
    settings = get_settings().model_copy(update={"rate_limit_auth_per_minute": 2})
    body = {"email": "nobody-rate-limit@example.com", "password": "correct-horse-battery"}
    with TestClient(create_app(settings=settings)) as client:
        headers = {"X-CSRF-Token": _csrf(client)}
        first = client.post("/api/v1/auth/login", json=body, headers=headers)
        second = client.post("/api/v1/auth/login", json=body, headers=headers)
        third = client.post("/api/v1/auth/login", json=body, headers=headers)

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "rate_limited"


def test_cost_stays_blank_until_prices_are_set() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://localhost/chatbot",
    )

    assert _provider_cost(settings, "gemini", 1_000_000, 2_000_000) is None

    priced = settings.model_copy(
        update={
            "gemini_input_usd_per_million": 0.1,
            "gemini_output_usd_per_million": 0.4,
        }
    )
    assert _provider_cost(priced, "gemini", 1_000_000, 1_000_000) == Decimal("0.5")
