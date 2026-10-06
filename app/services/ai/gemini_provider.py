import logging
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
from google import genai
from google.genai.errors import APIError
from google.genai.types import HttpOptions, HttpRetryOptions

from app.core.config import Settings
from app.core.errors import AppError
from app.services.ai.provider import ChatTurn, ModelCompletion, StreamItem, TokenUsage

logger = logging.getLogger(__name__)


class _Interactions(Protocol):
    async def create(self, **kwargs: object) -> object: ...


class _AsyncApi(Protocol):
    interactions: _Interactions


class _GeminiClient(Protocol):
    aio: _AsyncApi


def _not_configured() -> AppError:
    return AppError(
        code="gemini_not_configured",
        message="The model is not configured.",
        status_code=503,
    )


def _provider_error(code: str, message: str, status_code: int) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)


def gemini_request(turns: list[ChatTurn]) -> tuple[list[dict[str, object]], str | None]:
    """Map stored turns into Gemini steps and one system instruction."""
    if not turns or any(not turn.content.strip() for turn in turns):
        raise AppError(
            code="validation_error",
            message="A model request needs message text.",
            status_code=422,
        )

    system_parts: list[str] = []
    steps: list[dict[str, object]] = []
    for turn in turns:
        if turn.role == "system":
            system_parts.append(turn.content.strip())
            continue
        step_type = "user_input" if turn.role == "user" else "model_output"
        steps.append(
            {
                "type": step_type,
                "content": [{"type": "text", "text": turn.content}],
            }
        )
    if not steps:
        raise AppError(
            code="validation_error",
            message="A model request needs message text.",
            status_code=422,
        )
    instruction = "\n\n".join(system_parts) if system_parts else None
    return steps, instruction


def _map_gemini_exception(exc: Exception) -> AppError | None:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return _provider_error("gemini_timeout", "The model timed out.", 504)
    if isinstance(exc, APIError):
        logger.warning("gemini status error status=%s", exc.code)
        if exc.code == 429:
            return _provider_error(
                "gemini_rate_limited",
                "The model is busy. Try again shortly.",
                429,
            )
        if exc.code in {401, 403}:
            return _not_configured()
        if 500 <= exc.code < 600:
            return _provider_error(
                "gemini_unavailable",
                "The model could not complete the request.",
                502,
            )
        return _provider_error("gemini_error", "The model failed to respond.", 502)
    if isinstance(exc, httpx.RequestError):
        return _provider_error(
            "gemini_unavailable",
            "The model could not be reached.",
            502,
        )
    return None


def _usage_from(usage: object | None) -> TokenUsage | None:
    if usage is None:
        return None
    input_tokens = getattr(usage, "total_input_tokens", None)
    output_tokens = getattr(usage, "total_output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    return TokenUsage(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        total_tokens=int(total_tokens),
    )


def _end_status(status: object) -> str:
    if status == "completed":
        return "complete"
    if status in {"incomplete", "cancelled", "budget_exceeded"}:
        return "incomplete"
    raise _provider_error("gemini_error", "The model failed to respond.", 502)


def _stream_item(event: object) -> StreamItem | None:
    event_type = getattr(event, "event_type", None)
    if event_type == "step.delta":
        delta = getattr(event, "delta", None)
        if getattr(delta, "type", None) != "text":
            return None
        text = str(getattr(delta, "text", ""))
        if not text:
            return None
        return StreamItem(type="delta", text=text)
    if event_type == "error":
        error = getattr(event, "error", None)
        logger.warning("gemini stream error code=%s", getattr(error, "code", None))
        raise _provider_error("gemini_error", "The model failed to respond.", 502)
    if event_type == "interaction.status_update":
        status = getattr(event, "status", None)
        if status in {"failed", "requires_action"}:
            logger.warning("gemini stream status=%s", status)
            raise _provider_error("gemini_error", "The model failed to respond.", 502)
        return None
    if event_type == "interaction.completed":
        interaction = getattr(event, "interaction", None)
        status = _end_status(getattr(interaction, "status", None))
        return StreamItem(
            type="end",
            status=status,  # type: ignore[arg-type]
            usage=_usage_from(getattr(interaction, "usage", None)),
        )
    return None


class GeminiProvider:
    """Calls the Gemini Interactions API. The API key never leaves this class."""

    def __init__(self, settings: Settings, *, client: _GeminiClient | None = None) -> None:
        self._settings = settings
        self._client = client

    async def complete(self, turns: list[ChatTurn]) -> ModelCompletion:
        model = self._model()
        steps, instruction = gemini_request(turns)
        client = self._client_or_create()
        try:
            response = await client.aio.interactions.create(
                **self._request(model, steps, instruction, stream=False),
            )
        except Exception as exc:
            mapped = _map_gemini_exception(exc)
            if mapped is None:
                raise
            raise mapped from None

        status = getattr(response, "status", "completed")
        if status == "failed":
            logger.warning("gemini response status=%s", status)
            raise _provider_error("gemini_error", "The model failed to respond.", 502)
        return ModelCompletion(
            response_id=str(getattr(response, "id", "")),
            model=str(getattr(response, "model", model)),
            text=str(getattr(response, "output_text", "") or ""),
            usage=_usage_from(getattr(response, "usage", None)),
        )

    async def stream(self, turns: list[ChatTurn]) -> AsyncIterator[StreamItem]:
        model = self._model()
        steps, instruction = gemini_request(turns)
        client = self._client_or_create()
        try:
            sdk_stream = await client.aio.interactions.create(
                **self._request(model, steps, instruction, stream=True),
            )
        except Exception as exc:
            mapped = _map_gemini_exception(exc)
            if mapped is None:
                raise
            raise mapped from None

        try:
            async for event in sdk_stream:
                try:
                    item = _stream_item(event)
                except AppError:
                    raise
                except Exception as exc:
                    mapped = _map_gemini_exception(exc)
                    if mapped is None:
                        raise
                    raise mapped from None
                if item is None:
                    continue
                yield item
                if item.type == "end":
                    return
        except AppError:
            raise
        except Exception as exc:
            mapped = _map_gemini_exception(exc)
            if mapped is None:
                raise
            raise mapped from None
        finally:
            close = getattr(sdk_stream, "close", None)
            if close is not None:
                await close()
        yield StreamItem(type="end", status="complete")

    def _model(self) -> str:
        model = self._settings.gemini_model.strip()
        key = self._settings.gemini_api_key.get_secret_value().strip()
        if not model or not key:
            raise _not_configured()
        return model

    def _request(
        self,
        model: str,
        steps: list[dict[str, object]],
        instruction: str | None,
        *,
        stream: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model,
            "input": steps,
            "store": False,
            "stream": stream,
            "timeout": self._settings.gemini_timeout_seconds,
        }
        if instruction is not None:
            payload["system_instruction"] = instruction
        return payload

    def _client_or_create(self) -> _GeminiClient:
        if self._client is None:
            self._client = genai.Client(
                api_key=self._settings.gemini_api_key.get_secret_value(),
                http_options=HttpOptions(
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
        return self._client
