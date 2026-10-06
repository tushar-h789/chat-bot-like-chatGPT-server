import logging
from types import SimpleNamespace

import httpx
import pytest
from google.genai.errors import ClientError, ServerError
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import AppError
from app.services.ai.gemini_provider import GeminiProvider, gemini_request
from app.services.ai.provider import ChatDocument, ChatImage, ChatTurn, StreamItem, TokenUsage
from app.services.ai.tools import gemini_tools


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/chatbot",
        "gemini_api_key": SecretStr("unit-test-gemini-key"),
        "gemini_model": "gemini-3.8-flash",
        "gemini_timeout_seconds": 12,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class _Interactions:
    def __init__(
        self,
        result: object | None = None,
        error: Exception | None = None,
        results: list[object] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.results = list(results) if results is not None else None
        self.kwargs: dict[str, object] | None = None
        self.payloads: list[dict[str, object]] = []
        self.calls = 0

    async def create(self, **kwargs: object) -> object:
        self.calls += 1
        self.kwargs = kwargs
        self.payloads.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.results is not None:
            return self.results.pop(0)
        return self.result


class _Client:
    def __init__(self, interactions: _Interactions) -> None:
        self.aio = SimpleNamespace(interactions=interactions)


class _SdkStream:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.closed = False

    def __aiter__(self):
        async def generate():
            for event in self._events:
                yield event

        return generate()

    async def close(self) -> None:
        self.closed = True


def _usage() -> SimpleNamespace:
    return SimpleNamespace(total_input_tokens=4, total_output_tokens=6, total_tokens=10)


def test_gemini_request_maps_roles_and_keeps_one_system_instruction() -> None:
    steps, instruction = gemini_request(
        [
            ChatTurn(role="system", content="Be brief."),
            ChatTurn(role="system", content="Use plain text."),
            ChatTurn(role="user", content="What is React?"),
            ChatTurn(role="assistant", content="React is a library."),
            ChatTurn(role="user", content="What about Server Components?"),
        ]
    )

    assert instruction == "Be brief.\n\nUse plain text."
    assert steps == [
        {"type": "user_input", "content": [{"type": "text", "text": "What is React?"}]},
        {
            "type": "model_output",
            "content": [{"type": "text", "text": "React is a library."}],
        },
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "What about Server Components?"}],
        },
    ]


def test_gemini_request_includes_image_bytes() -> None:
    steps, _instruction = gemini_request(
        [
            ChatTurn(
                role="user",
                content="What color is this?",
                images=[ChatImage(mime_type="image/png", data=b"hi")],
            )
        ]
    )

    assert steps[0]["content"] == [
        {"type": "text", "text": "What color is this?"},
        {"type": "image", "mime_type": "image/png", "data": "aGk="},
    ]


def test_gemini_request_includes_pdf_bytes() -> None:
    steps, _instruction = gemini_request(
        [
            ChatTurn(
                role="user",
                content="What is in this PDF?",
                documents=[
                    ChatDocument(name="notes.pdf", mime_type="application/pdf", data=b"hi")
                ],
            )
        ]
    )

    assert steps[0]["content"] == [
        {"type": "text", "text": "What is in this PDF?"},
        {"type": "document", "mime_type": "application/pdf", "data": "aGk="},
    ]


@pytest.mark.asyncio
async def test_web_search_adds_the_google_search_tool() -> None:
    sdk_stream = _SdkStream(
        [
            SimpleNamespace(
                event_type="interaction.completed",
                interaction=SimpleNamespace(status="completed", usage=None),
            )
        ]
    )
    interactions = _Interactions(sdk_stream)
    provider = GeminiProvider(_settings(), client=_Client(interactions))  # type: ignore[arg-type]

    items = [
        item
        async for item in provider.stream(
            [ChatTurn(role="user", content="Look this up", web_search=True)]
        )
    ]

    assert interactions.kwargs is not None
    assert interactions.kwargs["tools"] == gemini_tools(web_search=True)
    assert items[-1].type == "end"


def test_gemini_request_omits_system_instruction_when_there_is_none() -> None:
    steps, instruction = gemini_request([ChatTurn(role="user", content="Hi")])

    assert instruction is None
    assert steps[0]["type"] == "user_input"


def test_gemini_request_rejects_blank_or_system_only_turns() -> None:
    with pytest.raises(AppError) as blank:
        gemini_request([ChatTurn(role="user", content="   ")])
    assert blank.value.status_code == 422

    with pytest.raises(AppError) as system_only:
        gemini_request([ChatTurn(role="system", content="Only a rule.")])
    assert system_only.value.status_code == 422


@pytest.mark.asyncio
async def test_stream_maps_text_deltas_and_usage() -> None:
    sdk_stream = _SdkStream(
        [
            SimpleNamespace(event_type="step.start"),
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="Hel"),
            ),
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="thought", text="skip"),
            ),
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="lo"),
            ),
            SimpleNamespace(
                event_type="interaction.completed",
                interaction=SimpleNamespace(status="completed", usage=_usage()),
            ),
        ]
    )
    interactions = _Interactions(sdk_stream)
    provider = GeminiProvider(_settings(), client=_Client(interactions))  # type: ignore[arg-type]

    items = [
        item
        async for item in provider.stream(
            [
                ChatTurn(role="system", content="Be brief."),
                ChatTurn(role="user", content="Hi"),
            ]
        )
    ]

    assert interactions.kwargs is not None
    assert interactions.kwargs["model"] == "gemini-3.8-flash"
    assert interactions.kwargs["store"] is False
    assert interactions.kwargs["stream"] is True
    assert interactions.kwargs["timeout"] == 12
    assert interactions.kwargs["system_instruction"] == "Be brief."
    assert interactions.kwargs["tools"] == gemini_tools()
    assert items == [
        StreamItem(type="delta", text="Hel"),
        StreamItem(type="delta", text="lo"),
        StreamItem(
            type="end",
            status="complete",
            usage=TokenUsage(input_tokens=4, output_tokens=6, total_tokens=10),
        ),
    ]
    assert sdk_stream.closed is True


@pytest.mark.asyncio
async def test_incomplete_stream_keeps_the_partial_status() -> None:
    sdk_stream = _SdkStream(
        [
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="Partial"),
            ),
            SimpleNamespace(
                event_type="interaction.completed",
                interaction=SimpleNamespace(status="incomplete", usage=None),
            ),
        ]
    )
    provider = GeminiProvider(
        _settings(),
        client=_Client(_Interactions(sdk_stream)),  # type: ignore[arg-type]
    )

    items = [item async for item in provider.stream([ChatTurn(role="user", content="Hi")])]

    assert items[-1] == StreamItem(type="end", status="incomplete")


@pytest.mark.asyncio
async def test_missing_configuration_does_not_call_gemini() -> None:
    interactions = _Interactions(SimpleNamespace())
    provider = GeminiProvider(
        _settings(gemini_api_key=SecretStr(""), gemini_model=""),
        client=_Client(interactions),  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as caught:
        await provider.complete([ChatTurn(role="user", content="Hi")])

    assert caught.value.code == "gemini_not_configured"
    assert interactions.calls == 0


class APITimeoutError(Exception):
    """Same name as the Interactions client's timeout, which is not builtin TimeoutError."""


class _NotFound(Exception):
    """Same module prefix as an unmapped Interactions client error."""


_NotFound.__module__ = "google.genai.errors"


class _RateLimitError(Exception):
    pass


_RateLimitError.__name__ = "RateLimitError"
_RateLimitError.__module__ = "google.genai.errors"


class _InteractionsStatus(Exception):
    """Stand-in for the Interactions client's APIError, which is a different class."""

    def __init__(self, code: int) -> None:
        super().__init__("upstream-secret")
        self.code = code


@pytest.mark.asyncio
async def test_timeout_rate_limit_and_auth_map_to_safe_errors(
    caplog: logging.LogCaptureFixture,
) -> None:
    cases = [
        (httpx.TimeoutException("upstream-secret"), "gemini_timeout", 504),
        (
            ClientError(429, {"error": {"message": "upstream-secret", "status": "RESOURCE_EXHAUSTED"}}),
            "gemini_rate_limited",
            429,
        ),
        (
            ClientError(401, {"error": {"message": "upstream-secret", "status": "UNAUTHENTICATED"}}),
            "gemini_not_configured",
            503,
        ),
        (
            ServerError(503, {"error": {"message": "upstream-secret", "status": "UNAVAILABLE"}}),
            "gemini_unavailable",
            502,
        ),
        (_InteractionsStatus(503), "gemini_unavailable", 502),
        (APITimeoutError("upstream-secret"), "gemini_timeout", 504),
        (_NotFound("upstream-secret"), "gemini_unavailable", 502),
        (_RateLimitError("upstream-secret"), "gemini_rate_limited", 429),
    ]

    with caplog.at_level(logging.WARNING):
        for error, code, status_code in cases:
            interactions = _Interactions(error=error)
            provider = GeminiProvider(_settings(), client=_Client(interactions))  # type: ignore[arg-type]
            with pytest.raises(AppError) as caught:
                await provider.complete([ChatTurn(role="user", content="Hi")])
            assert caught.value.code == code
            assert caught.value.status_code == status_code
            assert "upstream-secret" not in caught.value.message

    assert "upstream-secret" not in caplog.text


@pytest.mark.asyncio
async def test_stream_error_event_hides_the_upstream_message(
    caplog: logging.LogCaptureFixture,
) -> None:
    sdk_stream = _SdkStream(
        [
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="Partial"),
            ),
            SimpleNamespace(
                event_type="error",
                error=SimpleNamespace(code="internal", message="upstream-secret"),
            ),
        ]
    )
    provider = GeminiProvider(
        _settings(),
        client=_Client(_Interactions(sdk_stream)),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(AppError) as caught:
            async for _item in provider.stream([ChatTurn(role="user", content="Hi")]):
                pass

    assert caught.value.code == "gemini_error"
    assert "upstream-secret" not in caught.value.message
    assert "upstream-secret" not in caplog.text
    assert sdk_stream.closed is True


@pytest.mark.asyncio
async def test_stream_runs_a_function_then_continues() -> None:
    first = _SdkStream(
        [
            SimpleNamespace(
                event_type="step.start",
                index=0,
                step=SimpleNamespace(type="thought", signature="sig-1"),
            ),
            SimpleNamespace(
                event_type="step.start",
                index=1,
                step=SimpleNamespace(
                    type="function_call",
                    id="call-1",
                    name="calculate",
                    arguments={"expression": "2+2"},
                ),
            ),
            SimpleNamespace(event_type="interaction.status_update", status="requires_action"),
        ]
    )
    second = _SdkStream(
        [
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="4"),
            ),
            SimpleNamespace(
                event_type="interaction.completed",
                interaction=SimpleNamespace(status="completed", usage=_usage(), steps=None),
            ),
        ]
    )
    interactions = _Interactions(results=[first, second])
    provider = GeminiProvider(_settings(), client=_Client(interactions))  # type: ignore[arg-type]

    items = [
        item
        async for item in provider.stream([ChatTurn(role="user", content="What is 2+2?")])
    ]

    assert items[0].type == "tool"
    assert items[0].tool is not None
    assert items[0].tool.result == "4"
    assert items[1] == StreamItem(type="delta", text="4")
    assert items[2].type == "end"
    assert items[2].usage == TokenUsage(input_tokens=4, output_tokens=6, total_tokens=10)
    follow = interactions.payloads[1]["input"]
    assert isinstance(follow, list)
    assert follow[-3] == {"type": "thought", "signature": "sig-1"}
    assert follow[-2]["type"] == "function_call"
    assert follow[-1] == {
        "type": "function_result",
        "call_id": "call-1",
        "name": "calculate",
        "result": [{"type": "text", "text": "4"}],
    }
    assert first.closed is True
    assert second.closed is True


@pytest.mark.asyncio
async def test_stream_joins_streamed_function_arguments() -> None:
    first = _SdkStream(
        [
            SimpleNamespace(
                event_type="step.start",
                index=1,
                step=SimpleNamespace(
                    type="function_call",
                    id="call-1",
                    name="calculate",
                    arguments={},
                ),
            ),
            SimpleNamespace(
                event_type="step.delta",
                index=1,
                delta=SimpleNamespace(type="arguments_delta", arguments='{"expression":'),
            ),
            SimpleNamespace(
                event_type="step.delta",
                index=1,
                delta=SimpleNamespace(type="arguments_delta", arguments='"(17 + 3) * 2"}'),
            ),
            SimpleNamespace(event_type="interaction.status_update", status="requires_action"),
        ]
    )
    second = _SdkStream(
        [
            SimpleNamespace(
                event_type="step.delta",
                delta=SimpleNamespace(type="text", text="40"),
            ),
            SimpleNamespace(
                event_type="interaction.completed",
                interaction=SimpleNamespace(status="completed", usage=_usage(), steps=None),
            ),
        ]
    )
    interactions = _Interactions(results=[first, second])
    provider = GeminiProvider(_settings(), client=_Client(interactions))  # type: ignore[arg-type]

    items = [
        item
        async for item in provider.stream([ChatTurn(role="user", content="What is (17 + 3) * 2?")])
    ]

    assert items[0].type == "tool"
    assert items[0].tool is not None
    assert items[0].tool.result == "40"
    follow = interactions.payloads[1]["input"]
    assert isinstance(follow, list)
    assert follow[-2]["arguments"] == {"expression": "(17 + 3) * 2"}
    assert follow[-1]["result"] == [{"type": "text", "text": "40"}]


class _BadRequestError(Exception):
    pass


_BadRequestError.__name__ = "BadRequestError"
_BadRequestError.__module__ = "google.genai.errors"


@pytest.mark.asyncio
async def test_bad_request_is_not_retried() -> None:
    interactions = _Interactions(error=_BadRequestError("upstream-secret"))
    provider = GeminiProvider(_settings(), client=_Client(interactions))  # type: ignore[arg-type]

    with pytest.raises(AppError) as caught:
        await provider.complete([ChatTurn(role="user", content="Hi")])

    assert caught.value.code == "gemini_error"
    assert "upstream-secret" not in caught.value.message
    assert interactions.calls == 1


def test_real_client_does_not_retry() -> None:
    provider = GeminiProvider(_settings())
    client = provider._client_or_create()

    retry = client._api_client._http_options.retry_options  # type: ignore[attr-defined]
    assert retry is not None
    assert retry.attempts == 1
