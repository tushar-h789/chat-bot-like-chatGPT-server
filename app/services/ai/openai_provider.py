import base64
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
from app.services.ai.provider import ChatTurn, ModelCompletion, StreamItem, TokenUsage, ToolCall
from app.services.ai.tools import MAX_MODEL_ROUNDS, ToolOutcome, openai_tools, run_tool

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


def _merge_usage(left: TokenUsage | None, right: TokenUsage | None) -> TokenUsage | None:
    if left is None:
        return right
    if right is None:
        return left
    return TokenUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def _openai_call(item: object) -> dict[str, str] | None:
    if getattr(item, "type", None) != "function_call":
        return None
    call_id = str(getattr(item, "call_id", "") or "")
    name = str(getattr(item, "name", "") or "")
    if not call_id or not name:
        return None
    return {
        "call_id": call_id,
        "name": name,
        "arguments": str(getattr(item, "arguments", "") or ""),
    }


def _openai_calls(output: object) -> list[dict[str, str]]:
    if output is None:
        return []
    calls: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in output:
        call = _openai_call(item)
        if call is None or call["call_id"] in seen:
            continue
        seen.add(call["call_id"])
        calls.append(call)
    return calls


def _remember_call(calls: list[dict[str, str]], seen: set[str], call: dict[str, str] | None) -> None:
    if call is None or call["call_id"] in seen:
        return
    seen.add(call["call_id"])
    calls.append(call)


def _tool_item(outcome: ToolOutcome) -> StreamItem:
    return StreamItem(
        type="tool",
        tool=ToolCall(
            name=outcome.name,
            arguments=outcome.arguments,
            result=outcome.result,
        ),
    )


def _append_function_results(
    items: list[dict[str, object]],
    text: str,
    calls: list[dict[str, str]],
) -> list[ToolOutcome]:
    outcomes: list[ToolOutcome] = []
    if text.strip():
        items.append({"role": "assistant", "content": text})
    for call in calls:
        outcome = run_tool(call["name"], call["arguments"])
        outcomes.append(outcome)
        items.append(
            {
                "type": "function_call",
                "call_id": call["call_id"],
                "name": call["name"],
                "arguments": call["arguments"] or "{}",
            }
        )
        items.append(
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": outcome.result,
            }
        )
    return outcomes


def _request_kwargs(
    model: str,
    turns: list[ChatTurn],
    *,
    timeout: float,
    stream: bool = False,
    input_items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": model,
        "input": _input(turns) if input_items is None else input_items,
        "store": False,
        "timeout": timeout,
        "tools": openai_tools(web_search=any(turn.web_search for turn in turns)),
    }
    if stream:
        payload["stream"] = True
    return payload


def _input(turns: list[ChatTurn]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for turn in turns:
        if not turn.images and not turn.documents:
            items.append({"role": turn.role, "content": turn.content})
            continue
        content: list[dict[str, str]] = [{"type": "input_text", "text": turn.content}]
        for image in turn.images:
            encoded = base64.b64encode(image.data).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "detail": "auto",
                    "image_url": f"data:{image.mime_type};base64,{encoded}",
                }
            )
        for document in turn.documents:
            encoded = base64.b64encode(document.data).decode("ascii")
            content.append(
                {
                    "type": "input_file",
                    "filename": document.name,
                    "file_data": f"data:{document.mime_type};base64,{encoded}",
                }
            )
        items.append({"role": turn.role, "content": content})
    return items


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
        items = _input(turns)
        texts: list[str] = []
        usage: TokenUsage | None = None
        response: object | None = None
        try:
            for index in range(MAX_MODEL_ROUNDS):
                response = await client.responses.create(
                    **_request_kwargs(
                        model,
                        turns,
                        timeout=self._settings.openai_timeout_seconds,
                        input_items=items,
                    )
                )
                _raise_for_response_error(getattr(response, "error", None))
                text = str(getattr(response, "output_text", "") or "")
                texts.append(text)
                usage = _merge_usage(usage, _usage_from(getattr(response, "usage", None)))
                calls = _openai_calls(getattr(response, "output", None))
                if not calls or index == MAX_MODEL_ROUNDS - 1:
                    break
                _append_function_results(items, text, calls)
        except Exception as exc:
            mapped = _map_openai_exception(exc)
            if mapped is None:
                raise
            raise mapped from None

        assert response is not None
        return ModelCompletion(
            response_id=str(getattr(response, "id", "")),
            model=str(getattr(response, "model", "")),
            text="".join(texts),
            usage=usage,
        )

    async def stream(self, turns: list[ChatTurn]) -> AsyncIterator[StreamItem]:
        model = self._model()
        _require_text(turns)
        client = self._client_or_create()
        items = _input(turns)
        usage: TokenUsage | None = None
        for index in range(MAX_MODEL_ROUNDS):
            try:
                sdk_stream = await client.responses.create(
                    **_request_kwargs(
                        model,
                        turns,
                        timeout=self._settings.openai_timeout_seconds,
                        stream=True,
                        input_items=items,
                    )
                )
            except Exception as exc:
                mapped = _map_openai_exception(exc)
                if mapped is None:
                    raise
                raise mapped from None

            calls: list[dict[str, str]] = []
            seen: set[str] = set()
            text_parts: list[str] = []
            end: StreamItem | None = None
            try:
                async for event in sdk_stream:
                    event_type = getattr(event, "type", None)
                    if event_type == "response.output_item.done":
                        _remember_call(calls, seen, _openai_call(getattr(event, "item", None)))
                    if event_type in {"response.completed", "response.incomplete"}:
                        response = getattr(event, "response", None)
                        usage = _merge_usage(
                            usage, _usage_from(getattr(response, "usage", None))
                        )
                        for call in _openai_calls(getattr(response, "output", None)):
                            _remember_call(calls, seen, call)
                    item = _stream_item(event)
                    if item is None:
                        continue
                    if item.type == "delta" and item.text:
                        text_parts.append(item.text)
                        yield item
                    elif item.type == "end":
                        end = StreamItem(type="end", status=item.status, usage=usage)
                        break
            except Exception as exc:
                mapped = _map_openai_exception(exc)
                if mapped is None:
                    raise
                raise mapped from None
            finally:
                close = getattr(sdk_stream, "close", None)
                if close is not None:
                    await close()
            if calls and index < MAX_MODEL_ROUNDS - 1:
                for outcome in _append_function_results(items, "".join(text_parts), calls):
                    yield _tool_item(outcome)
                continue
            if end is not None:
                yield StreamItem(
                    type="end",
                    status="incomplete" if calls else end.status,
                    usage=usage,
                )
                return
            yield StreamItem(
                type="end",
                status="incomplete" if calls else "complete",
                usage=usage,
            )
            return

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
