import asyncio
import json
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.ai.provider import ChatTurn, StreamItem, TokenUsage
from tests.test_auth import _csrf, _register
from tests.test_chat_stream import FakeProvider, _delete_user, _events


def _reply(text: str, total: int) -> list[StreamItem]:
    return [
        StreamItem(type="delta", text=text),
        StreamItem(
            type="end",
            status="complete",
            usage=TokenUsage(input_tokens=1, output_tokens=total - 1, total_tokens=total),
        ),
    ]


@pytest.fixture
def emails() -> Iterator[tuple[str, str]]:
    values = (
        f"regen-{uuid4()}@example.com",
        f"regen-{uuid4()}@example.com",
    )
    yield values
    for value in values:
        asyncio.run(_delete_user(value))


def test_regenerate_replaces_the_latest_reply(emails: tuple[str, str]) -> None:
    provider = FakeProvider(_reply("Hello", 5))
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, emails[0])
        headers = {"X-CSRF-Token": _csrf(client)}
        first = client.post(
            "/api/v1/chat",
            json={"content": "Hi there"},
            headers=headers,
        )
        conversation_id = json.loads(_events(first.text)[0][1])["id"]
        provider.items = _reply("Second reply", 7)
        client.post(
            "/api/v1/chat",
            json={"content": "Second", "conversation_id": conversation_id},
            headers=headers,
        )
        listed = client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assistant_id = listed.json()[3]["id"]
        provider.items = _reply("Fresh", 2)
        regenerated = client.post(
            "/api/v1/chat/regenerate",
            json={"conversation_id": conversation_id, "message_id": assistant_id},
            headers=headers,
        )
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")
        usage = client.get("/api/v1/usage")

    assert regenerated.status_code == 200
    assert json.loads(_events(regenerated.text)[-1][1])["message_id"] == assistant_id
    assert provider.turns == [
        ChatTurn(role="user", content="Hi there"),
        ChatTurn(role="assistant", content="Hello"),
        ChatTurn(role="user", content="Second"),
    ]
    rows = messages.json()
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "Hi there"),
        ("assistant", "Hello"),
        ("user", "Second"),
        ("assistant", "Fresh"),
    ]
    assert rows[3]["id"] == assistant_id
    assert usage.json()["replies"] == 2
    assert usage.json()["total_tokens"] == 7


def test_only_the_latest_reply_can_be_regenerated(emails: tuple[str, str]) -> None:
    provider = FakeProvider(_reply("Hello", 5))
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, emails[0])
        headers = {"X-CSRF-Token": _csrf(client)}
        first = client.post(
            "/api/v1/chat",
            json={"content": "Hi there"},
            headers=headers,
        )
        conversation_id = json.loads(_events(first.text)[0][1])["id"]
        listed = client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assistant_id = listed.json()[1]["id"]
        client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "Later"},
            headers=headers,
        )
        rejected = client.post(
            "/api/v1/chat/regenerate",
            json={"conversation_id": conversation_id, "message_id": assistant_id},
            headers=headers,
        )
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "not_regenerable"
    assert messages.json()[1]["content"] == "Hello"


def test_another_user_cannot_regenerate(emails: tuple[str, str]) -> None:
    provider = FakeProvider(_reply("Hello", 5))
    with TestClient(create_app(ai_provider=provider)) as owner:
        _register(owner, emails[0])
        headers = {"X-CSRF-Token": _csrf(owner)}
        first = owner.post(
            "/api/v1/chat",
            json={"content": "Hi there"},
            headers=headers,
        )
        conversation_id = json.loads(_events(first.text)[0][1])["id"]
        listed = owner.get(f"/api/v1/conversations/{conversation_id}/messages")
        assistant_id = listed.json()[1]["id"]

    with TestClient(create_app(ai_provider=provider)) as stranger:
        _register(stranger, emails[1])
        rejected = stranger.post(
            "/api/v1/chat/regenerate",
            json={"conversation_id": conversation_id, "message_id": assistant_id},
            headers={"X-CSRF-Token": _csrf(stranger)},
        )

    assert rejected.status_code == 404
    assert rejected.json()["error"]["code"] == "not_found"
