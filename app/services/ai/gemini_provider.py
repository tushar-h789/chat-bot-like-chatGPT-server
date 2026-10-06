import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
from google import genai
from google.genai.errors import APIError
from google.genai.types import HttpOptions, HttpRetryOptions

from app.core.config import Settings
from app.core.errors import AppError
from app.services.ai.provider import ChatTurn, ModelCompletion, StreamItem, TokenUsage, ToolCall
from app.services.ai.tools import MAX_MODEL_ROUNDS, ToolOutcome, gemini_tools, run_tool

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
        content: list[dict[str, object]] = [{"type": "text", "text": turn.content}]
        for image in turn.images:
            content.append(
                {
                    "type": "image",
                    "mime_type": image.mime_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                }
            )
        for document in turn.documents:
            content.append(
                {
                    "type": "document",
                    "mime_type": document.mime_type,
                    "data": base64.b64encode(document.data).decode("ascii"),
                }
            )
        steps.append({"type": step_type, "content": content})
    if not steps:
        raise AppError(
            code="validation_error",
            message="A model request needs message text.",
            status_code=422,
        )
    instruction = "\n\n".join(system_parts) if system_parts else None
    return steps, instruction


def _status_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None


def _is_timeout(exc: Exception) -> bool:
    # The Interactions client raises its own APITimeoutError, not the builtin one.
    return isinstance(exc, (httpx.TimeoutException, TimeoutError)) or type(exc).__name__ in {
        "APITimeoutError",
        "TimeoutException",
    }


def _map_gemini_exception(exc: Exception) -> AppError | None:
    if _is_timeout(exc):
        logger.warning("gemini timeout error type=%s", type(exc).__name__)
        return _provider_error("gemini_timeout", "The model timed out.", 504)
    # The Interactions client raises its own APIError, not google.genai.errors.APIError.
    status = _status_code(exc)
    if isinstance(exc, APIError) or status is not None:
        logger.warning("gemini status error status=%s", status)
        if status == 429:
            return _provider_error(
                "gemini_rate_limited",
                "The model is busy. Try again shortly.",
                429,
            )
        if status in {401, 403}:
            return _not_configured()
        if status is not None and 500 <= status < 600:
            return _provider_error(
                "gemini_unavailable",
                "The model could not complete the request.",
                502,
            )
        return _provider_error("gemini_error", "The model failed to respond.", 502)
    if (
        type(exc).__name__ == "BadRequestError"
        and type(exc).__module__.startswith("google.genai")
    ):
        logger.warning("gemini client error type=BadRequestError")
        return _provider_error("gemini_error", "The model failed to respond.", 502)
    if (
        type(exc).__name__ == "RateLimitError"
        and type(exc).__module__.startswith("google.genai")
    ):
        logger.warning("gemini status error status=429")
        return _provider_error(
            "gemini_rate_limited",
            "The model is busy. Try again shortly.",
            429,
        )
    if isinstance(exc, httpx.RequestError):
        return _provider_error(
            "gemini_unavailable",
            "The model could not be reached.",
            502,
        )
    if type(exc).__module__.startswith("google.genai"):
        logger.warning("gemini client error type=%s", type(exc).__name__)
        return _provider_error(
            "gemini_unavailable",
            "The model could not complete the request.",
            502,
        )
    return None


_RETRYABLE = {"gemini_timeout", "gemini_unavailable", "gemini_rate_limited"}


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


def _argument_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _function_call(step: object) -> dict[str, object] | None:
    if getattr(step, "type", None) != "function_call":
        return None
    call_id = str(getattr(step, "id", "") or "")
    name = str(getattr(step, "name", "") or "")
    if not call_id or not name:
        return None
    return {
        "id": call_id,
        "name": name,
        "arguments": _argument_object(getattr(step, "arguments", None)),
    }


def _remember_call(
    calls: list[dict[str, object]],
    by_id: dict[str, dict[str, object]],
    call_at: dict[int, str],
    call: dict[str, object],
    step_index: int | None,
) -> None:
    call_id = str(call["id"])
    if step_index is not None:
        call_at[step_index] = call_id
    current = by_id.get(call_id)
    if current is None:
        by_id[call_id] = call
        calls.append(call)
        return
    arguments = call["arguments"]
    if isinstance(arguments, dict) and arguments and not current["arguments"]:
        current["arguments"] = arguments


def _collect_stream_call(
    event: object,
    calls: list[dict[str, object]],
    by_id: dict[str, dict[str, object]],
    call_at: dict[int, str],
    argument_parts: dict[int, list[str]],
) -> None:
    """Keep one function call, including arguments that arrive as text fragments."""
    event_type = getattr(event, "event_type", None)
    step_index = getattr(event, "index", None)
    index = step_index if isinstance(step_index, int) else None
    if event_type == "step.delta":
        delta = getattr(event, "delta", None)
        if getattr(delta, "type", None) != "arguments_delta" or index is None:
            return
        piece = getattr(delta, "arguments", None)
        if isinstance(piece, str) and piece:
            argument_parts.setdefault(index, []).append(piece)
        return
    for call in _calls_on(event):
        _remember_call(
            calls,
            by_id,
            call_at,
            call,
            index if event_type == "step.start" else None,
        )


def _apply_argument_parts(
    by_id: dict[str, dict[str, object]],
    call_at: dict[int, str],
    argument_parts: dict[int, list[str]],
) -> None:
    for step_index, parts in argument_parts.items():
        call_id = call_at.get(step_index)
        if call_id is None:
            continue
        parsed = _argument_object("".join(parts))
        if parsed:
            by_id[call_id]["arguments"] = parsed


def _collect_model_step(
    event: object,
    model_steps: dict[int, dict[str, object]],
    signature_parts: dict[int, list[str]],
) -> None:
    """Keep thought and function steps so the follow-up request can replay them."""
    event_type = getattr(event, "event_type", None)
    raw_index = getattr(event, "index", None)
    index = raw_index if isinstance(raw_index, int) else None
    if event_type == "step.start":
        if index is None:
            index = max(model_steps, default=-1) + 1
        dumped = _dump_step(getattr(event, "step", None))
        if dumped is not None and dumped.get("type") != "user_input":
            model_steps[index] = dumped
        return
    if event_type == "step.delta" and index is not None:
        delta = getattr(event, "delta", None)
        if getattr(delta, "type", None) != "thought_signature":
            return
        piece = getattr(delta, "signature", None)
        if isinstance(piece, str) and piece:
            signature_parts.setdefault(index, []).append(piece)
        return
    if event_type != "interaction.completed":
        return
    interaction = getattr(event, "interaction", None)
    finished: list[dict[str, object]] = []
    for step in getattr(interaction, "steps", None) or []:
        dumped = _dump_step(step)
        if dumped is not None and dumped.get("type") != "user_input":
            finished.append(dumped)
    useful = any(
        (step.get("type") == "function_call" and step.get("arguments"))
        or (step.get("type") == "thought" and step.get("signature"))
        for step in finished
    )
    if not useful:
        return
    model_steps.clear()
    signature_parts.clear()
    model_steps.update({position: step for position, step in enumerate(finished)})


def _finish_model_steps(
    model_steps: dict[int, dict[str, object]],
    by_id: dict[str, dict[str, object]],
    signature_parts: dict[int, list[str]],
) -> None:
    for index, parts in signature_parts.items():
        step = model_steps.get(index)
        if step is None or step.get("type") != "thought" or step.get("signature"):
            continue
        step["signature"] = "".join(parts)
    for step in model_steps.values():
        if step.get("type") != "function_call":
            continue
        call = by_id.get(str(step.get("id", "")))
        if call is None:
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, dict) and arguments:
            step["arguments"] = arguments


def _calls_on(event: object) -> list[dict[str, object]]:
    event_type = getattr(event, "event_type", None)
    if event_type == "step.start":
        call = _function_call(getattr(event, "step", None))
        return [call] if call is not None else []
    if event_type == "interaction.completed":
        interaction = getattr(event, "interaction", None)
        steps = getattr(interaction, "steps", None) or []
        return [call for step in steps if (call := _function_call(step)) is not None]
    return []


def _needs_action(event: object) -> bool:
    event_type = getattr(event, "event_type", None)
    if event_type == "interaction.status_update":
        return getattr(event, "status", None) == "requires_action"
    if event_type == "interaction.completed":
        interaction = getattr(event, "interaction", None)
        return getattr(interaction, "status", None) == "requires_action"
    return False


def _tool_item(outcome: ToolOutcome) -> StreamItem:
    return StreamItem(
        type="tool",
        tool=ToolCall(
            name=outcome.name,
            arguments=outcome.arguments,
            result=outcome.result,
        ),
    )


def _dump_step(step: object) -> dict[str, object] | None:
    """Copy one model step so the next request can send it back unchanged."""
    dump = getattr(step, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=True)
        except TypeError:
            data = dump()
        if isinstance(data, dict) and isinstance(data.get("type"), str):
            return data
    step_type = getattr(step, "type", None)
    if step_type == "function_call":
        call = _function_call(step)
        if call is None:
            return None
        return {"type": "function_call", **call}
    if step_type == "thought":
        data: dict[str, object] = {"type": "thought"}
        signature = getattr(step, "signature", None)
        if isinstance(signature, str) and signature:
            data["signature"] = signature
        return data
    if step_type == "model_output":
        content = getattr(step, "content", None)
        if isinstance(content, list):
            return {"type": "model_output", "content": content}
        return {"type": "model_output"}
    return None


def _function_result_step(call_id: str, name: str, outcome: ToolOutcome) -> dict[str, object]:
    result: dict[str, object] = {
        "type": "function_result",
        "call_id": call_id,
        "name": name,
        "result": [{"type": "text", "text": outcome.result}],
    }
    if outcome.is_error:
        result["is_error"] = True
    return result


def _append_function_results(
    steps: list[dict[str, object]],
    text: str,
    pairs: list[tuple[dict[str, object], ToolOutcome]],
    model_steps: dict[int, dict[str, object]] | None = None,
) -> None:
    ordered = [model_steps[index] for index in sorted(model_steps or {})]
    if text.strip() and not any(step.get("type") == "model_output" for step in ordered):
        steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    replayed = {
        str(step.get("id"))
        for step in ordered
        if step.get("type") == "function_call" and step.get("id")
    }
    steps.extend(ordered)
    for call, outcome in pairs:
        call_id = str(call["id"])
        if call_id not in replayed:
            steps.append(
                {
                    "type": "function_call",
                    "id": call_id,
                    "name": call["name"],
                    "arguments": call["arguments"],
                }
            )
        steps.append(_function_result_step(call_id, str(call["name"]), outcome))


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
        if status == "failed":
            logger.warning("gemini stream status=%s", status)
            raise _provider_error("gemini_error", "The model failed to respond.", 502)
        return None
    if event_type == "interaction.completed":
        interaction = getattr(event, "interaction", None)
        status = getattr(interaction, "status", None)
        if status == "requires_action":
            return None
        status = _end_status(status)
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
        web_search = any(turn.web_search for turn in turns)
        texts: list[str] = []
        usage: TokenUsage | None = None
        response: object | None = None
        for index in range(MAX_MODEL_ROUNDS):
            response = await self._create(
                client,
                self._request(
                    model,
                    steps,
                    instruction,
                    stream=False,
                    web_search=web_search,
                ),
            )
            status = getattr(response, "status", "completed")
            if status == "failed":
                logger.warning("gemini response status=%s", status)
                raise _provider_error("gemini_error", "The model failed to respond.", 502)
            text = str(getattr(response, "output_text", "") or "")
            texts.append(text)
            usage = _merge_usage(usage, _usage_from(getattr(response, "usage", None)))
            calls = [
                call
                for step in (getattr(response, "steps", None) or [])
                if (call := _function_call(step)) is not None
            ]
            if status != "requires_action" or not calls or index == MAX_MODEL_ROUNDS - 1:
                break
            pairs = [(call, run_tool(str(call["name"]), call["arguments"])) for call in calls]
            model_steps: dict[int, dict[str, object]] = {}
            for position, step in enumerate(getattr(response, "steps", None) or []):
                dumped = _dump_step(step)
                if dumped is not None and dumped.get("type") != "user_input":
                    model_steps[position] = dumped
            _append_function_results(steps, text, pairs, model_steps)
        assert response is not None
        return ModelCompletion(
            response_id=str(getattr(response, "id", "")),
            model=str(getattr(response, "model", model)),
            text="".join(texts),
            usage=usage,
        )

    async def stream(self, turns: list[ChatTurn]) -> AsyncIterator[StreamItem]:
        model = self._model()
        steps, instruction = gemini_request(turns)
        client = self._client_or_create()
        web_search = any(turn.web_search for turn in turns)
        usage: TokenUsage | None = None
        for index in range(MAX_MODEL_ROUNDS):
            sdk_stream = await self._create(
                client,
                self._request(
                    model,
                    steps,
                    instruction,
                    stream=True,
                    web_search=web_search,
                ),
            )
            calls: list[dict[str, object]] = []
            by_id: dict[str, dict[str, object]] = {}
            call_at: dict[int, str] = {}
            argument_parts: dict[int, list[str]] = {}
            model_steps: dict[int, dict[str, object]] = {}
            signature_parts: dict[int, list[str]] = {}
            text_parts: list[str] = []
            end: StreamItem | None = None
            needs_action = False
            try:
                async for event in sdk_stream:
                    try:
                        needs_action = needs_action or _needs_action(event)
                        if getattr(event, "event_type", None) == "interaction.completed":
                            interaction = getattr(event, "interaction", None)
                            usage = _merge_usage(
                                usage,
                                _usage_from(getattr(interaction, "usage", None)),
                            )
                        _collect_stream_call(event, calls, by_id, call_at, argument_parts)
                        _collect_model_step(event, model_steps, signature_parts)
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
                    if item.type == "delta" and item.text:
                        text_parts.append(item.text)
                        yield item
                    elif item.type == "end" and not needs_action:
                        end = StreamItem(type="end", status=item.status, usage=usage)
                        break
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
            _apply_argument_parts(by_id, call_at, argument_parts)
            _finish_model_steps(model_steps, by_id, signature_parts)
            pending = needs_action or (bool(calls) and end is None)
            if pending and not calls:
                raise _provider_error("gemini_error", "The model failed to respond.", 502)
            if pending and index < MAX_MODEL_ROUNDS - 1:
                pairs = [
                    (call, run_tool(str(call["name"]), call["arguments"])) for call in calls
                ]
                for _call, outcome in pairs:
                    yield _tool_item(outcome)
                _append_function_results(steps, "".join(text_parts), pairs, model_steps)
                continue
            if end is not None:
                yield end
                return
            yield StreamItem(
                type="end",
                status="incomplete" if calls else "complete",
                usage=usage,
            )
            return

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
        web_search: bool,
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
        payload["tools"] = gemini_tools(web_search=web_search)
        return payload

    async def _create(self, client: _GeminiClient, payload: dict[str, object]) -> object:
        """One retry covers a short Gemini capacity spike without hiding a real failure."""
        mapped: AppError | None = None
        for attempt in (1, 2):
            try:
                return await client.aio.interactions.create(**payload)
            except Exception as exc:
                mapped = _map_gemini_exception(exc)
                if mapped is None or mapped.code not in _RETRYABLE or attempt == 2:
                    if mapped is None:
                        raise
                    raise mapped from None
                await asyncio.sleep(0.4)
        if mapped is not None:
            raise mapped
        raise _provider_error("gemini_error", "The model failed to respond.", 502)

    def _client_or_create(self) -> _GeminiClient:
        if self._client is None:
            timeout_ms = int(self._settings.gemini_timeout_seconds * 1000)
            self._client = genai.Client(
                api_key=self._settings.gemini_api_key.get_secret_value(),
                http_options=HttpOptions(
                    timeout=timeout_ms,
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
        return self._client
