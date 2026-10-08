import asyncio
from collections.abc import Iterator
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from tests.test_auth import PASSWORD, _csrf, _register


def _postgres_dsn() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _delete_user(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute("DELETE FROM users WHERE email = $1", email)
    finally:
        await connection.close()


async def _set_admin(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute(
            "UPDATE users SET is_admin = true WHERE email = $1",
            email,
        )
    finally:
        await connection.close()


@pytest.fixture
def email() -> Iterator[str]:
    value = f"admin-{uuid4()}@example.com"
    yield value
    asyncio.run(_delete_user(value))


def _settings(*, admin_emails: str = "") -> Settings:
    return Settings(
        _env_file=None,
        database_url=get_settings().database_url,
        admin_emails=admin_emails,
    )


def test_admin_stats_requires_a_session() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/admin/stats")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_admin_stats_forbids_a_normal_user(email: str) -> None:
    with TestClient(create_app()) as client:
        _register(client, email)
        response = client.get("/api/v1/admin/stats")
        me = client.get("/api/v1/auth/me")

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "forbidden",
            "message": "Admin access is required.",
        }
    }
    assert me.status_code == 200
    assert me.json()["is_admin"] is False
    assert me.json()["email"] == email


def test_admin_stats_allows_a_flagged_admin(email: str) -> None:
    other = f"admin-{uuid4()}@example.com"
    try:
        with TestClient(create_app()) as client:
            _register(client, email)
            with TestClient(create_app()) as second:
                _register(second, other)
            asyncio.run(_set_admin(email))
            stats = client.get("/api/v1/admin/stats")
            me = client.get("/api/v1/auth/me")

        assert me.status_code == 200
        assert me.json()["is_admin"] is True
        assert stats.status_code == 200
        body = stats.json()
        assert body["total_users"] >= 2
        assert body["active_sessions"] >= 2
        assert body["users_with_usage"] >= 0
        assert body["total_tokens"] >= 0
        emails = {row["email"] for row in body["users"]}
        assert email in emails
        assert other in emails
        assert all("password" not in row for row in body["users"])
        assert all("password_hash" not in row for row in body["users"])
    finally:
        asyncio.run(_delete_user(other))


def test_admin_stats_allows_an_allowlisted_email(email: str) -> None:
    with TestClient(create_app(_settings(admin_emails=email))) as client:
        registered = _register(client, email)
        me = registered.get("/api/v1/auth/me")
        stats = registered.get("/api/v1/admin/stats")

    assert me.status_code == 200
    assert me.json()["is_admin"] is True
    assert stats.status_code == 200
    assert stats.json()["total_users"] >= 1
    assert any(row["email"] == email for row in stats.json()["users"])


def test_admin_emails_can_list_more_than_one_address() -> None:
    settings = _settings(admin_emails=" Ada@Example.com , other@example.com ")

    assert settings.admin_email_set == frozenset(
        {"ada@example.com", "other@example.com"}
    )
