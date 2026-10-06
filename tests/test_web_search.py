import asyncio
import json
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.ai.provider import ChatTurn, StreamItem
from tests.test_auth import _csrf, _register
from tests.test_chat_stream import FakeProvider, _delete_user, _events


@pytest.fixture
def email() -> Iterator[str]:
    value = f"search-{uuid4()}@example.com"
    yield value
    asyncio.run(_delete_user(value))


class _Recording(FakeProvider):
    def __init__(self, items: list[StreamItem]) -> None:
        super().__init__(items)
        self.searches: list[bool] = []

    async def stream(self, turns: list[ChatTurn]):
        self.searches.append(bool(turns) and turns[-1].web_search)
        async for item in super().stream(turns):
            yield item


def test_web_search_is_stored_and_reused_on_regenerate(email: str) -> None:
    provider = _Recording(
        [
            StreamItem(type="delta", text="Found"),
            StreamItem(type="end", status="complete", usage=None),
        ]
    )
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, email)
        headers = {"X-CSRF-Token": _csrf(client)}
        sent = client.post(
            "/api/v1/chat",
            json={"content": "Look this up", "web_search": True},
            headers=headers,
        )
        conversation_id = json.loads(_events(sent.text)[0][1])["id"]
        listed = client.get(f"/api/v1/conversations/{conversation_id}/messages")
        regenerated = client.post(
            "/api/v1/chat/regenerate",
            json={
                "conversation_id": conversation_id,
                "message_id": listed.json()[1]["id"],
            },
            headers=headers,
        )
        plain = client.post(
            "/api/v1/chat",
            json={"content": "No search"},
            headers=headers,
        )

    assert sent.status_code == 200
    assert listed.json()[0]["web_search"] is True
    assert listed.json()[1]["web_search"] is False
    assert regenerated.status_code == 200
    assert plain.status_code == 200
    assert provider.searches == [True, True, False]
