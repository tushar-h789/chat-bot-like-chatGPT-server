import asyncio
import json
from collections.abc import Iterator
from uuid import uuid4

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.ai.provider import StreamItem, TokenUsage
from tests.test_auth import _csrf, _register
from tests.test_chat_stream import FakeProvider, _events, _postgres_dsn


def _reply(total: int) -> list[StreamItem]:
    return [
        StreamItem(type="delta", text="ok"),
        StreamItem(
            type="end",
            status="complete",
            usage=TokenUsage(input_tokens=1, output_tokens=total - 1, total_tokens=total),
        ),
    ]


async def _delete_user(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute("DELETE FROM users WHERE email = $1", email)
    finally:
        await connection.close()


async def _backdate_usage(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute(
            """
            UPDATE usage_events
            SET created_at = now() - interval '2 days'
            WHERE user_id = (SELECT id FROM users WHERE email = $1)
            """,
            email,
        )
    finally:
        await connection.close()


@pytest.fixture
def email() -> Iterator[str]:
    value = f"usage-limit-{uuid4()}@example.com"
    yield value
    asyncio.run(_delete_user(value))


def test_daily_limit_blocks_the_next_chat(email: str) -> None:
    settings = get_settings().model_copy(update={"usage_limit_tokens_per_day": 5})
    provider = FakeProvider(_reply(5))
    with TestClient(create_app(settings=settings, ai_provider=provider)) as client:
        _register(client, email)
        headers = {"X-CSRF-Token": _csrf(client)}
        first = client.post("/api/v1/chat", json={"content": "Hi"}, headers=headers)
        usage = client.get("/api/v1/usage")
        second = client.post("/api/v1/chat", json={"content": "Again"}, headers=headers)
        events = _events(first.text)
        conversation_id = json.loads(events[0][1])["id"]
        message_id = json.loads(events[-1][1])["message_id"]
        regenerated = client.post(
            "/api/v1/chat/regenerate",
            json={"conversation_id": conversation_id, "message_id": message_id},
            headers=headers,
        )
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")

    assert first.status_code == 200
    assert usage.json()["tokens_today"] == 5
    assert usage.json()["daily_token_limit"] == 5
    assert usage.json()["total_tokens"] == 5
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "usage_limited"
    assert regenerated.status_code == 429
    assert regenerated.json()["error"]["code"] == "usage_limited"
    assert messages.json()[1]["content"] == "ok"


def test_tokens_from_earlier_days_do_not_count(email: str) -> None:
    settings = get_settings().model_copy(update={"usage_limit_tokens_per_day": 5})
    provider = FakeProvider(_reply(5))
    with TestClient(create_app(settings=settings, ai_provider=provider)) as client:
        _register(client, email)
        headers = {"X-CSRF-Token": _csrf(client)}
        client.post("/api/v1/chat", json={"content": "Yesterday"}, headers=headers)
        asyncio.run(_backdate_usage(email))
        allowed = client.post("/api/v1/chat", json={"content": "Today"}, headers=headers)
        usage = client.get("/api/v1/usage")

    assert allowed.status_code == 200
    assert usage.json()["total_tokens"] == 10
    assert usage.json()["tokens_today"] == 5


def test_zero_turns_the_daily_cap_off(email: str) -> None:
    settings = get_settings().model_copy(update={"usage_limit_tokens_per_day": 0})
    provider = FakeProvider(_reply(5))
    with TestClient(create_app(settings=settings, ai_provider=provider)) as client:
        _register(client, email)
        headers = {"X-CSRF-Token": _csrf(client)}
        first = client.post("/api/v1/chat", json={"content": "Hi"}, headers=headers)
        second = client.post("/api/v1/chat", json={"content": "Again"}, headers=headers)
        usage = client.get("/api/v1/usage")

    assert first.status_code == 200
    assert second.status_code == 200
    assert usage.json()["daily_token_limit"] is None
    assert usage.json()["tokens_today"] == 10
