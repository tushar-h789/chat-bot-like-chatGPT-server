import asyncio
import json
from collections.abc import Iterator
from uuid import uuid4

from types import SimpleNamespace

import asyncpg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.v1.chat import _next_item
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.main import create_app
from app.services.ai.gemini_provider import GeminiProvider
from app.services.ai.provider import ChatTurn, StreamItem, TokenUsage
from tests.test_auth import _csrf, _register


def _postgres_dsn() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _delete_user(email: str) -> None:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        await connection.execute("DELETE FROM users WHERE email = $1", email)
    finally:
        await connection.close()


async def _messages(conversation_id: str) -> list[dict[str, object]]:
    connection = await asyncpg.connect(_postgres_dsn())
    try:
        rows = await connection.fetch(
            """
            SELECT role, content, status, metadata
            FROM messages
            WHERE conversation_id = $1
            ORDER BY created_at, id
            """,
            conversation_id,
        )
    finally:
        await connection.close()
    return [dict(row) for row in rows]


@pytest.fixture
def email() -> Iterator[str]:
    value = f"phase6-{uuid4()}@example.com"
    yield value
    asyncio.run(_delete_user(value))


class FakeProvider:
    def __init__(
        self,
        items: list[StreamItem],
        error: Exception | None = None,
    ) -> None:
        self.items = items
        self.error = error
        self.turns: list[ChatTurn] | None = None
        self.closed = False

    async def stream(self, turns: list[ChatTurn]):
        self.turns = list(turns)
        try:
            for item in self.items:
                yield item
            if self.error is not None:
                raise self.error
        finally:
            self.closed = True


def _events(body: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        event = "message"
        data = ""
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data = line.removeprefix("data:").strip()
        events.append((event, data))
    return events


def test_chat_stream_saves_the_reply(email: str) -> None:
    provider = FakeProvider(
        [
            StreamItem(type="delta", text="Hello"),
            StreamItem(type="delta", text=" there"),
            StreamItem(
                type="end",
                status="complete",
                usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5),
            ),
        ]
    )
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, email)
        response = client.post(
            "/api/v1/chat",
            json={"content": "Hi there"},
            headers={"X-CSRF-Token": _csrf(client)},
        )
        usage_response = client.get("/api/v1/usage")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [event for event, _data in events] == ["conversation", "delta", "delta", "done"]
    assert '"text":"Hello"' in events[1][1]
    assert '"text":" there"' in events[2][1]
    assert '"status":"complete"' in events[3][1]
    assert provider.closed is True
    assert provider.turns == [ChatTurn(role="user", content="Hi there")]

    conversation_id = events[0][1].split('"id":"')[1].split('"')[0]
    rows = asyncio.run(_messages(conversation_id))
    assert [(row["role"], row["content"], row["status"]) for row in rows] == [
        ("user", "Hi there", "complete"),
        ("assistant", "Hello there", "complete"),
    ]
    metadata = rows[1]["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["usage"]["total_tokens"] == 5
    assert usage_response.status_code == 200
    assert usage_response.json()["total_tokens"] == 5
    assert usage_response.json()["replies"] == 1
    assert usage_response.json()["cost_usd"] is None


class _GeminiCall:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = 0

    def provider(self, event_groups: list[list[object]]) -> GeminiProvider:
        recorder = self

        class GeminiStream:
            def __init__(self, events: list[object]) -> None:
                self._events = events

            def __aiter__(self):
                async def generate():
                    for event in self._events:
                        yield event

                return generate()

            async def close(self) -> None:
                recorder.closed += 1

        class Interactions:
            async def create(self, **kwargs: object) -> GeminiStream:
                recorder.calls.append(kwargs)
                return GeminiStream(event_groups[len(recorder.calls) - 1])

        return GeminiProvider(
            Settings(
                _env_file=None,  # type: ignore[arg-type]
                database_url="postgresql+asyncpg://localhost/unused",
                gemini_api_key=SecretStr("unit-test-gemini-key"),
                gemini_model="gemini-3.8-flash",
            ),
            client=SimpleNamespace(aio=SimpleNamespace(interactions=Interactions())),  # type: ignore[arg-type]
        )


def _text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        event_type="step.delta",
        delta=SimpleNamespace(type="text", text=text),
    )


def _completed(usage: SimpleNamespace | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        event_type="interaction.completed",
        interaction=SimpleNamespace(status="completed", usage=usage),
    )


def test_gemini_stream_uses_the_existing_sse_events(email: str) -> None:
    gemini = _GeminiCall()
    provider = gemini.provider(
        [[_text_delta("Hello"), _text_delta(" there"), _completed()]]
    )
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, email)
        response = client.post(
            "/api/v1/chat",
            json={"content": "Hi there"},
            headers={"X-CSRF-Token": _csrf(client)},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [name for name, _data in events] == ["conversation", "delta", "delta", "done"]
    assert [json.loads(data)["text"] for name, data in events if name == "delta"] == [
        "Hello",
        " there",
    ]
    assert json.loads(events[-1][1])["status"] == "complete"
    assert gemini.calls[0]["stream"] is True
    assert gemini.calls[0]["store"] is False
    assert gemini.calls[0]["model"] == "gemini-3.8-flash"
    assert gemini.closed == 1


def test_gemini_reply_is_stored_once_and_reused_as_context(email: str) -> None:
    gemini = _GeminiCall()
    provider = gemini.provider(
        [
            [
                _text_delta("Hello"),
                _text_delta(" there"),
                _completed(
                    SimpleNamespace(
                        total_input_tokens=2,
                        total_output_tokens=3,
                        total_tokens=5,
                    )
                ),
            ],
            [_text_delta("They run on the server."), _completed()],
        ]
    )
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, email)
        first = client.post(
            "/api/v1/chat",
            json={"content": "What is React?"},
            headers={"X-CSRF-Token": _csrf(client)},
        )
        conversation_id = json.loads(_events(first.text)[0][1])["id"]
        second = client.post(
            "/api/v1/chat",
            json={
                "content": "What about Server Components?",
                "conversation_id": conversation_id,
            },
            headers={"X-CSRF-Token": _csrf(client)},
        )

    rows = asyncio.run(_messages(conversation_id))
    assert [(row["role"], row["content"], row["status"]) for row in rows] == [
        ("user", "What is React?", "complete"),
        ("assistant", "Hello there", "complete"),
        ("user", "What about Server Components?", "complete"),
        ("assistant", "They run on the server.", "complete"),
    ]
    metadata = rows[1]["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["usage"]["total_tokens"] == 5
    assert second.status_code == 200
    assert gemini.calls[1]["input"] == [
        {"type": "user_input", "content": [{"type": "text", "text": "What is React?"}]},
        {"type": "model_output", "content": [{"type": "text", "text": "Hello there"}]},
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "What about Server Components?"}],
        },
    ]


def test_gemini_failure_keeps_the_user_message_and_one_partial_reply(email: str) -> None:
    gemini = _GeminiCall()
    provider = gemini.provider(
        [
            [
                _text_delta("Partial"),
                SimpleNamespace(
                    event_type="error",
                    error=SimpleNamespace(code="internal", message="upstream-secret"),
                ),
            ]
        ]
    )
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, email)
        response = client.post(
            "/api/v1/chat",
            json={"content": "Explain hooks"},
            headers={"X-CSRF-Token": _csrf(client)},
        )

    events = _events(response.text)
    assert [name for name, _data in events] == ["conversation", "delta", "error"]
    error = json.loads(events[-1][1])
    assert error["code"] == "gemini_error"
    assert "upstream-secret" not in error["message"]
    conversation_id = json.loads(events[0][1])["id"]
    rows = asyncio.run(_messages(conversation_id))
    assert [(row["role"], row["content"], row["status"]) for row in rows] == [
        ("user", "Explain hooks", "complete"),
        ("assistant", "Partial", "incomplete"),
    ]
    assert gemini.closed == 1


def test_chat_stream_keeps_a_partial_reply_when_the_model_fails(email: str) -> None:
    provider = FakeProvider(
        [StreamItem(type="delta", text="Partial")],
        error=AppError(
            code="openai_timeout",
            message="The model timed out.",
            status_code=504,
        ),
    )
    with TestClient(create_app(ai_provider=provider)) as client:
        _register(client, email)
        response = client.post(
            "/api/v1/chat",
            json={"content": "Continue"},
            headers={"X-CSRF-Token": _csrf(client)},
        )

    events = _events(response.text)
    assert events[-1][0] == "error"
    assert "openai_timeout" in events[-1][1]
    conversation_id = events[0][1].split('"id":"')[1].split('"')[0]
    rows = asyncio.run(_messages(conversation_id))
    assert rows[1]["content"] == "Partial"
    assert rows[1]["status"] == "incomplete"


def test_chat_requires_a_session() -> None:
    with TestClient(create_app(ai_provider=FakeProvider([]))) as client:
        response = client.post("/api/v1/chat", json={"content": "Hi"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_chat_stream_rejects_another_users_conversation(email: str) -> None:
    other = f"phase6-{uuid4()}@example.com"
    provider = FakeProvider([StreamItem(type="end")])
    try:
        with TestClient(create_app(ai_provider=provider)) as owner:
            _register(owner, email)
            created = owner.post(
                "/api/v1/conversations",
                json={"title": "Mine"},
                headers={"X-CSRF-Token": _csrf(owner)},
            )
            conversation_id = created.json()["id"]

        with TestClient(create_app(ai_provider=provider)) as stranger:
            _register(stranger, other)
            response = stranger.post(
                "/api/v1/chat",
                json={"content": "Nope", "conversation_id": conversation_id},
                headers={"X-CSRF-Token": _csrf(stranger)},
            )
    finally:
        asyncio.run(_delete_user(other))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert provider.turns is None


@pytest.mark.asyncio
async def test_disconnect_cancels_a_blocked_model_stream() -> None:
    class DisconnectAfter:
        def __init__(self) -> None:
            self.checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks > 2

    async def blocks():
        yield StreamItem(type="delta", text="Hello")
        await asyncio.Event().wait()

    iterator = blocks().__aiter__()
    first = await _next_item(iterator, DisconnectAfter())  # type: ignore[arg-type]
    assert isinstance(first, StreamItem)
    assert first.text == "Hello"
    second = await _next_item(iterator, DisconnectAfter())  # type: ignore[arg-type]
    assert second is None
    await iterator.aclose()
