from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health_reports_ok_when_database_accepts_queries() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_hides_database_failures() -> None:
    settings = Settings(
        environment="test",
        database_url="postgresql+asyncpg://chatbot:secret-password@127.0.0.1:1/chatbot",
        cors_origins="http://localhost:3000",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "database_unavailable",
            "message": "Database is unavailable.",
        }
    }
    assert "secret-password" not in response.text


def test_cors_allows_the_configured_frontend_origin() -> None:
    with TestClient(create_app()) as client:
        response = client.options(
            "/api/v1/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"
