import asyncio
from collections.abc import Iterator
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

PASSWORD = "correct-horse-battery"


def _postgres_dsn() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _delete_user(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute("DELETE FROM users WHERE email = $1", email)
    finally:
        await connection.close()


async def _expire_sessions(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute(
            """
            UPDATE sessions
            SET expires_at = now() - interval '1 day'
            WHERE user_id = (SELECT id FROM users WHERE email = $1)
            """,
            email,
        )
    finally:
        await connection.close()


async def _password_hash(email: str) -> str:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        value = await connection.fetchval(
            "SELECT password_hash FROM users WHERE email = $1",
            email,
        )
    finally:
        await connection.close()
    assert isinstance(value, str)
    return value


@pytest.fixture
def email() -> Iterator[str]:
    value = f"phase3-{uuid4()}@example.com"
    yield value
    asyncio.run(_delete_user(value))


def _csrf(client: TestClient) -> str:
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    assert isinstance(token, str) and token
    return token


def _register(client: TestClient, email: str, password: str = PASSWORD) -> TestClient:
    token = _csrf(client)
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 201, response.text
    return client


def test_register_logs_in_and_stores_a_password_hash(email: str) -> None:
    with TestClient(create_app()) as client:
        token = _csrf(client)
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email.upper(), "password": PASSWORD},
            headers={"X-CSRF-Token": token},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == email
        assert "password" not in body
        assert PASSWORD not in response.text
        session_cookie = response.headers.get("set-cookie", "")
        assert "session=" in session_cookie
        assert "HttpOnly" in session_cookie

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["id"] == body["id"]

    stored_hash = asyncio.run(_password_hash(email))
    assert stored_hash != PASSWORD
    assert stored_hash.startswith("$argon2")


def test_register_rejects_a_short_password_without_echoing_it(email: str) -> None:
    with TestClient(create_app()) as client:
        token = _csrf(client)
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "short-pass"},
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "short-pass" not in response.text


def test_register_rejects_a_duplicate_email(email: str) -> None:
    with TestClient(create_app()) as client:
        _register(client, email)
        token = _csrf(client)
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD},
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "email_already_registered",
            "message": "An account with this email already exists.",
        }
    }


def test_login_and_logout_round_trip(email: str) -> None:
    with TestClient(create_app()) as client:
        _register(client, email)
        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": _csrf(client)},
        )
        assert logout.status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": PASSWORD},
            headers={"X-CSRF-Token": _csrf(client)},
        )
        assert login.status_code == 200
        assert login.json()["email"] == email
        assert client.get("/api/v1/auth/me").status_code == 200


def test_login_uses_the_same_error_for_unknown_and_wrong_password(email: str) -> None:
    with TestClient(create_app()) as client:
        _register(client, email)
        token = _csrf(client)
        unknown = client.post(
            "/api/v1/auth/login",
            json={"email": f"missing-{email}", "password": PASSWORD},
            headers={"X-CSRF-Token": token},
        )
        wrong = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "incorrect-password-1"},
            headers={"X-CSRF-Token": token},
        )

    assert unknown.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["error"]["code"] == "invalid_credentials"
    assert "incorrect-password-1" not in wrong.text


def test_me_and_logout_require_a_session() -> None:
    with TestClient(create_app()) as client:
        me = client.get("/api/v1/auth/me")
        token = _csrf(client)
        logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": token})

    assert me.status_code == 401
    assert me.json()["error"]["code"] == "unauthenticated"
    assert logout.status_code == 401


def test_unsafe_auth_requests_require_csrf(email: str) -> None:
    with TestClient(create_app()) as client:
        client.get("/api/v1/auth/csrf")
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_expired_session_is_rejected(email: str) -> None:
    with TestClient(create_app()) as client:
        _register(client, email)
        asyncio.run(_expire_sessions(email))
        response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_users_cannot_read_each_other(email: str) -> None:
    other_email = f"phase3-{uuid4()}@example.com"
    try:
        with TestClient(create_app()) as first, TestClient(create_app()) as second:
            _register(first, email)
            _register(second, other_email)
            first_me = first.get("/api/v1/auth/me")
            second_me = second.get("/api/v1/auth/me")

        assert first_me.json()["email"] == email
        assert second_me.json()["email"] == other_email
        assert first_me.json()["id"] != second_me.json()["id"]
    finally:
        asyncio.run(_delete_user(other_email))
