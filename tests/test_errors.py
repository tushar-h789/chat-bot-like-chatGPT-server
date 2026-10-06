from pydantic import BaseModel
from fastapi.testclient import TestClient

from app.main import create_app


class _NameBody(BaseModel):
    name: str


def test_validation_errors_use_the_public_error_shape() -> None:
    app = create_app()

    @app.post("/api/v1/_validation_probe")
    def validation_probe(_body: _NameBody) -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/_validation_probe",
            json={"password": "secret-password"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "validation_error",
            "message": "Request validation failed.",
        }
    }
    assert "secret-password" not in response.text


def test_unexpected_errors_hide_internal_details() -> None:
    app = create_app()

    @app.get("/api/v1/_boom")
    def boom() -> None:
        raise RuntimeError("postgres://chatbot:secret-password@localhost/chatbot")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/_boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Something went wrong.",
        }
    }
    assert "secret-password" not in response.text
