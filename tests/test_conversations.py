import asyncio
from collections.abc import Iterator
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from tests.test_auth import PASSWORD, _csrf, _register

MISSING_ID = "00000000-0000-4000-8000-000000000001"


def _postgres_dsn() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _delete_user(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute("DELETE FROM users WHERE email = $1", email)
    finally:
        await connection.close()


async def _message_count(conversation_id: str) -> int:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        count = await connection.fetchval(
            "SELECT count(*) FROM messages WHERE conversation_id = $1",
            UUID(conversation_id),
        )
    finally:
        await connection.close()
    assert isinstance(count, int)
    return count


@pytest.fixture
def emails() -> Iterator[tuple[str, str]]:
    values = (
        f"phase4-{uuid4()}@example.com",
        f"phase4-{uuid4()}@example.com",
    )
    yield values
    for value in values:
        asyncio.run(_delete_user(value))


def _headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": _csrf(client)}


def test_conversation_and_message_lifecycle(emails: tuple[str, str]) -> None:
    owner, _other = emails
    with TestClient(create_app()) as client:
        _register(client, owner)
        created = client.post(
            "/api/v1/conversations",
            json={},
            headers=_headers(client),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        conversation_id = body["id"]
        assert body["title"] == "New chat"
        assert body["created_at"]
        assert body["updated_at"]

        renamed = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            json={"title": "  Trip plan  "},
            headers=_headers(client),
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["title"] == "Trip plan"

        listed = client.get("/api/v1/conversations")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [conversation_id]

        fetched = client.get(f"/api/v1/conversations/{conversation_id}")
        assert fetched.status_code == 200
        assert fetched.json()["title"] == "Trip plan"

        message = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "  Hello  ", "role": "assistant"},
            headers=_headers(client),
        )
        assert message.status_code == 201, message.text
        saved = message.json()
        assert saved["role"] == "user"
        assert saved["content"] == "  Hello  "
        assert saved["status"] == "complete"
        assert saved["conversation_id"] == conversation_id
        assert "password" not in saved

        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert messages.status_code == 200
        assert [item["id"] for item in messages.json()] == [saved["id"]]

        after_message = client.get(f"/api/v1/conversations/{conversation_id}")
        assert after_message.json()["updated_at"] >= renamed.json()["updated_at"]

        deleted = client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=_headers(client),
        )
        assert deleted.status_code == 204
        assert deleted.content == b""
        assert client.get(f"/api/v1/conversations/{conversation_id}").status_code == 404

    assert asyncio.run(_message_count(conversation_id)) == 0


def test_conversations_are_private_to_the_owner(emails: tuple[str, str]) -> None:
    owner, other = emails
    with TestClient(create_app()) as owner_client, TestClient(create_app()) as other_client:
        _register(owner_client, owner)
        created = owner_client.post(
            "/api/v1/conversations",
            json={"title": "Private notes"},
            headers=_headers(owner_client),
        )
        conversation_id = created.json()["id"]
        owner_client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "secret"},
            headers=_headers(owner_client),
        )

        _register(other_client, other)
        assert other_client.get("/api/v1/conversations").json() == []
        for method, path, payload in (
            ("GET", f"/api/v1/conversations/{conversation_id}", None),
            ("GET", f"/api/v1/conversations/{conversation_id}/messages", None),
            ("PATCH", f"/api/v1/conversations/{conversation_id}", {"title": "Stolen"}),
            ("DELETE", f"/api/v1/conversations/{conversation_id}", None),
            (
                "POST",
                f"/api/v1/conversations/{conversation_id}/messages",
                {"content": "nope"},
            ),
        ):
            response = other_client.request(
                method,
                path,
                json=payload,
                headers=_headers(other_client) if method != "GET" else None,
            )
            assert response.status_code == 404, response.text
            assert response.json()["error"]["code"] == "not_found"

        still_there = owner_client.get(f"/api/v1/conversations/{conversation_id}")
        assert still_there.status_code == 200
        assert still_there.json()["title"] == "Private notes"


def test_conversation_writes_require_auth_and_csrf(emails: tuple[str, str]) -> None:
    owner, _other = emails
    with TestClient(create_app()) as client:
        anonymous = client.post("/api/v1/conversations", json={"title": "Nope"})
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "unauthenticated"

        _register(client, owner)
        missing_csrf = client.post("/api/v1/conversations", json={"title": "Nope"})
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["error"]["code"] == "csrf_failed"

        created = client.post(
            "/api/v1/conversations",
            json={"title": "Kept"},
            headers=_headers(client),
        )
        conversation_id = created.json()["id"]
        blank = client.patch(
            f"/api/v1/conversations/{conversation_id}",
            json={"title": "   "},
            headers=_headers(client),
        )
        assert blank.status_code == 422
        assert blank.json()["error"]["code"] == "validation_error"

        too_long = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "a" * (get_settings().max_message_chars + 1)},
            headers=_headers(client),
        )
        assert too_long.status_code == 422
        assert too_long.json()["error"]["code"] == "validation_error"

        missing = client.get(f"/api/v1/conversations/{MISSING_ID}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"


def test_newest_conversation_is_listed_first(emails: tuple[str, str]) -> None:
    owner, _other = emails
    with TestClient(create_app()) as client:
        _register(client, owner)
        first = client.post(
            "/api/v1/conversations",
            json={"title": "Older"},
            headers=_headers(client),
        ).json()["id"]
        second = client.post(
            "/api/v1/conversations",
            json={"title": "Newer"},
            headers=_headers(client),
        ).json()["id"]
        client.patch(
            f"/api/v1/conversations/{first}",
            json={"title": "Touched"},
            headers=_headers(client),
        )
        listed = client.get("/api/v1/conversations")
        assert [item["id"] for item in listed.json()] == [first, second]
