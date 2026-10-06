import logging
from collections.abc import AsyncIterator
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.core.config import Settings
from app.core.errors import AppError
from app.services.ai.provider import ChatTurn, ModelCompletion, StreamItem, TokenUsage

logger = logging.getLogger(__name__)


class _ResponsesClient(Protocol):
    async def create(self, **kwargs: object) -> object: ...


class _OpenAIClient(Protocol):
    responses: _ResponsesClient


def _not_configured() -> AppError:
    return AppError(
        code="openai_not_configured",
        message="The model is not configured.",
        status_code=503,
    )


def _provider_error(code: str, message: str, status_code: int) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)


def _map_openai_exception(exc: Exception) -> AppError | None:
    if isinstance(exc, APITimeoutError):
        return _provider_error("openai_timeout", "The model timed out.", 504)
    if isinstance(exc, RateLimitError):
        return _provider_error(
            "openai_rate_limited",
            "The model is busy. Try again shortly.",
            429,
        )
    if isinstance(exc, AuthenticationError):
        return _not_configured()
    if isinstance(exc, APIStatusError):
        logger.warning("openai status error status=%s", exc.status_code)
        return _provider_error(
            "openai_unavailable",
            "The model could not complete the request.",
            502,
        )
    if isinstance(exc, APIConnectionError):
        return _provider_error(
            "openai_unavailable",
            "The model could not be reached.",
            502,
        )
    return None


def _usage_from(usage: object | None) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens"),
        output_tokens=getattr(usage, "output_tokens"),
        total_tokens=getattr(usage, "total_tokens"),
    )


def _require_text(turns: list[ChatTurn]) -> None:
    if not turns or any(not turn.content.strip() for turn in turns):
        raise AppError(
            code="validation_error",
            message="A model request needs message text.",
            status_code=422,
        )


def _input(turns: list[ChatTurn]) -> list[dict[str, str]]:
    return [{"role": turn.role, "content": turn.content} for turn in turns]


def _raise_for_response_error(error: object | None) -> None:
    if error is None:
        return
    error_code = getattr(error, "code", None)
    logger.warning("openai response error code=%s", error_code)
    if error_code == "rate_limit_exceeded":
        raise _provider_error(
            "openai_rate_limited",
            "The model is busy. Try again shortly.",
            429,
        )
    raise _provider_error("openai_error", "The model failed to respond.", 502)


def _stream_item(event: object) -> StreamItem | None:
    event_type = getattr(event, "type", None)
    if event_type == "response.output_text.delta":
        text = str(getattr(event, "delta", ""))
        if not text:
            return None
        return StreamItem(type="delta", text=text)
    if event_type == "response.completed":
        response = getattr(event, "response", None)
        return StreamItem(
            type="end",
            status="complete",
            usage=_usage_from(getattr(response, "usage", None)),
        )
    if event_type == "response.incomplete":
        response = getattr(event, "response", None)
        return StreamItem(
            type="end",
            status="incomplete",
            usage=_usage_from(getattr(response, "usage", None)),
        )
    if event_type == "error":
        logger.warning("openai stream error code=%s", getattr(event, "code", None))
        if getattr(event, "code", None) == "rate_limit_exceeded":
            raise _provider_error(
                "openai_rate_limited",
                "The model is busy. Try again shortly.",
                429,
            )
        raise _provider_error("openai_error", "The model failed to respond.", 502)
    if event_type == "response.failed":
        response = getattr(event, "response", None)
        error = getattr(response, "error", None)
        logger.warning("openai stream failed code=%s", getattr(error, "code", None))
        _raise_for_response_error(error or object())
    return None


class OpenAIProvider:
    """Calls the official Responses API. The API key never leaves this class."""

    def __init__(self, settings: Settings, *, client: _OpenAIClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def complete(self, turns: list[ChatTurn]) -> ModelCompletion:
        model = self._model()
        _require_text(turns)

        client = self._client_or_create()
        try:
            response = await client.responses.create(
                model=model,
                input=_input(turns),
                store=False,
                timeout=self._settings.openai_timeout_seconds,
            )
        except Exception as exc:
            mapped = _map_openai_exception(exc)
            if mapped is None:
                raise
            raise mapped from None

        _raise_for_response_error(getattr(response, "error", None))
        return ModelCompletion(
            response_id=str(response.id),
            model=str(response.model),
            text=str(response.output_text),
            usage=_usage_from(getattr(response, "usage", None)),
        )

    async def stream(self, turns: list[ChatTurn]) -> AsyncIterator[StreamItem]:
        model = self._model()
        _require_text(turns)
        client = self._client_or_create()
        try:
            sdk_stream = await client.responses.create(
                model=model,
                input=_input(turns),
                store=False,
                stream=True,
                timeout=self._settings.openai_timeout_seconds,
            )
        except Exception as exc:
            mapped = _map_openai_exception(exc)
            if mapped is None:
                raise
            raise mapped from None

        try:
            async for event in sdk_stream:
                item = _stream_item(event)
                if item is None:
                    continue
                yield item
                if item.type == "end":
                    return
        except Exception as exc:
            mapped = _map_openai_exception(exc)
            if mapped is None:
                raise
            raise mapped from None
        finally:
            close = getattr(sdk_stream, "close", None)
            if close is not None:
                await close()
        yield StreamItem(type="end", status="complete")

    def _model(self) -> str:
        model = self._settings.openai_model.strip()
        key = self._settings.openai_api_key.get_secret_value().strip()
        if not model or not key:
            raise _not_configured()
        return model

    def _client_or_create(self) -> _OpenAIClient:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._settings.openai_api_key.get_secret_value(),
                timeout=self._settings.openai_timeout_seconds,
            )
        return self._client
