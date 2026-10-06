import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import AppError
from app.services.ai.openai_provider import OpenAIProvider
from app.services.ai.provider import ChatImage, ChatTurn, StreamItem, TokenUsage


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/chatbot",
        "openai_api_key": SecretStr("unit-test-key"),
        "openai_model": "gpt-test",
        "openai_timeout_seconds": 12,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class _Responses:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.kwargs: dict[str, object] | None = None

    async def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _Client:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _completion() -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_test",
        model="gpt-test",
        output_text="Hello from the model",
        error=None,
        usage=SimpleNamespace(input_tokens=4, output_tokens=6, total_tokens=10),
    )


@pytest.mark.asyncio
async def test_complete_builds_a_responses_request() -> None:
    responses = _Responses(_completion())
    provider = OpenAIProvider(_settings(), client=_Client(responses))

    result = await provider.complete(
        [
            ChatTurn(role="system", content="Be brief."),
            ChatTurn(role="user", content="Hi"),
            ChatTurn(role="assistant", content="Hello"),
        ]
    )

    assert responses.kwargs == {
        "model": "gpt-test",
        "input": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ],
        "store": False,
        "timeout": 12,
    }
    assert "unit-test-key" not in str(responses.kwargs)
    assert result.response_id == "resp_test"
    assert result.model == "gpt-test"
    assert result.text == "Hello from the model"
    assert result.usage is not None
    assert result.usage.model_dump() == {
        "input_tokens": 4,
        "output_tokens": 6,
        "total_tokens": 10,
    }


@pytest.mark.asyncio
async def test_complete_sends_an_image_as_a_data_url() -> None:
    responses = _Responses(_completion())
    provider = OpenAIProvider(_settings(), client=_Client(responses))

    await provider.complete(
        [
            ChatTurn(
                role="user",
                content="What color is this?",
                images=[ChatImage(mime_type="image/png", data=b"hi")],
            )
        ]
    )

    assert responses.kwargs is not None
    assert responses.kwargs["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What color is this?"},
                {
                    "type": "input_image",
                    "detail": "auto",
                    "image_url": "data:image/png;base64,aGk=",
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_complete_adds_web_search_only_when_requested() -> None:
    responses = _Responses(_completion())
    provider = OpenAIProvider(_settings(), client=_Client(responses))

    await provider.complete(
        [ChatTurn(role="user", content="Look this up", web_search=True)]
    )

    assert responses.kwargs is not None
    assert responses.kwargs["tools"] == [{"type": "web_search"}]
    assert "web_search" not in str(responses.kwargs["input"])


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


@pytest.mark.asyncio
async def test_stream_maps_output_text_deltas() -> None:
    sdk_stream = _SdkStream(
        [
            SimpleNamespace(type="response.output_text.delta", delta="Hel"),
            SimpleNamespace(type="response.output_text.delta", delta="lo"),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3)
                ),
            ),
        ]
    )

    class _StreamingResponses:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        async def create(self, **kwargs: object) -> object:
            self.kwargs = kwargs
            return sdk_stream

    responses = _StreamingResponses()
    provider = OpenAIProvider(_settings(), client=_Client(responses))  # type: ignore[arg-type]

    items = [item async for item in provider.stream([ChatTurn(role="user", content="Hi")])]

    assert responses.kwargs is not None
    assert responses.kwargs["stream"] is True
    assert responses.kwargs["store"] is False
    assert items == [
        StreamItem(type="delta", text="Hel"),
        StreamItem(type="delta", text="lo"),
        StreamItem(
            type="end",
            status="complete",
            usage=TokenUsage(input_tokens=1, output_tokens=2, total_tokens=3),
        ),
    ]
    assert sdk_stream.closed is True


@pytest.mark.asyncio
async def test_complete_allows_a_response_without_usage() -> None:
    payload = _completion()
    payload.usage = None
    provider = OpenAIProvider(_settings(), client=_Client(_Responses(payload)))

    result = await provider.complete([ChatTurn(role="user", content="Hi")])

    assert result.usage is None
    assert result.text == "Hello from the model"


@pytest.mark.asyncio
async def test_missing_configuration_does_not_call_openai() -> None:
    responses = _Responses(_completion())
    provider = OpenAIProvider(
        _settings(openai_api_key=SecretStr(""), openai_model=""),
        client=_Client(responses),
    )

    with pytest.raises(AppError) as caught:
        await provider.complete([ChatTurn(role="user", content="Hi")])

    assert caught.value.code == "openai_not_configured"
    assert caught.value.status_code == 503
    assert responses.kwargs is None


@pytest.mark.asyncio
async def test_blank_turns_are_rejected() -> None:
    responses = _Responses(_completion())
    provider = OpenAIProvider(_settings(), client=_Client(responses))

    with pytest.raises(AppError) as caught:
        await provider.complete([ChatTurn(role="user", content="   ")])

    assert caught.value.code == "validation_error"
    assert responses.kwargs is None


@pytest.mark.asyncio
async def test_timeout_rate_limit_and_auth_map_to_safe_errors() -> None:
    cases = [
        (APITimeoutError(_request()), "openai_timeout", 504),
        (
            RateLimitError(
                "limit sk-test-secret-value",
                response=httpx.Response(429, request=_request()),
                body=None,
            ),
            "openai_rate_limited",
            429,
        ),
        (
            AuthenticationError(
                "bad key sk-test-secret-value",
                response=httpx.Response(401, request=_request()),
                body=None,
            ),
            "openai_not_configured",
            503,
        ),
    ]
    for error, code, status in cases:
        provider = OpenAIProvider(_settings(), client=_Client(_Responses(error=error)))
        with pytest.raises(AppError) as caught:
            await provider.complete([ChatTurn(role="user", content="Hi")])
        assert caught.value.code == code
        assert caught.value.status_code == status
        assert "sk-test-secret-value" not in caught.value.message


@pytest.mark.asyncio
async def test_connection_errors_hide_upstream_details(
    caplog: logging.LogCaptureFixture,
) -> None:
    error = APIConnectionError(message="dial sk-test-secret-value", request=_request())
    provider = OpenAIProvider(_settings(), client=_Client(_Responses(error=error)))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(AppError) as caught:
            await provider.complete([ChatTurn(role="user", content="Hi")])

    assert caught.value.code == "openai_unavailable"
    assert "sk-test-secret-value" not in caught.value.message
    assert "sk-test-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_status_errors_log_the_code_only(caplog: logging.LogCaptureFixture) -> None:
    error = APIStatusError(
        "upstream sk-test-secret-value",
        response=httpx.Response(500, request=_request()),
        body={"error": {"message": "sk-test-secret-value"}},
    )
    provider = OpenAIProvider(_settings(), client=_Client(_Responses(error=error)))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(AppError) as caught:
            await provider.complete([ChatTurn(role="user", content="Hi")])

    assert caught.value.code == "openai_unavailable"
    assert "sk-test-secret-value" not in caught.value.message
    assert "sk-test-secret-value" not in caplog.text
    assert "status=500" in caplog.text


@pytest.mark.asyncio
async def test_response_error_uses_the_code_only() -> None:
    payload = _completion()
    payload.error = SimpleNamespace(code="invalid_prompt", message="secret prompt text")
    provider = OpenAIProvider(_settings(), client=_Client(_Responses(payload)))

    with pytest.raises(AppError) as caught:
        await provider.complete([ChatTurn(role="user", content="Hi")])

    assert caught.value.code == "openai_error"
    assert "secret prompt text" not in caught.value.message
