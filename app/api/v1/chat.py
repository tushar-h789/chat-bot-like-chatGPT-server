import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_current_user, limit_chat, require_csrf
from app.core.errors import AppError
from app.db.models.user import User
from app.schemas.chat import ChatRequest, RegenerateRequest
from app.services.ai.provider import AIProvider, StreamItem, TokenUsage
from app.services.chat import ChatService, PreparedChat

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _next_item(
    iterator: AsyncIterator[StreamItem],
    request: Request,
) -> StreamItem | None | Literal["finished"]:
    task = asyncio.create_task(anext(iterator))
    try:
        while not task.done():
            if await request.is_disconnected():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, StopAsyncIteration):
                    return None
                return None
            await asyncio.wait({task}, timeout=0.05)
        try:
            return task.result()
        except StopAsyncIteration:
            return "finished"
    except asyncio.CancelledError:
        task.cancel()
        raise


def _stream(
    prepared: PreparedChat,
    request: Request,
    provider: AIProvider,
) -> StreamingResponse:
    session_factory = request.app.state.session_factory

    async def events() -> AsyncIterator[str]:
        yield _sse(
            "conversation",
            {"id": str(prepared.conversation_id), "title": prepared.title},
        )
        parts: list[str] = []
        status = "complete"
        usage: TokenUsage | None = None
        error: AppError | None = None
        iterator = provider.stream(prepared.turns).__aiter__()
        try:
            while True:
                try:
                    item = await _next_item(iterator, request)
                except AppError as exc:
                    error = exc
                    status = "incomplete"
                    break
                except Exception as exc:
                    logger.warning("chat stream failed error=%s", type(exc).__name__)
                    error = AppError(
                        code="model_unavailable",
                        message="The model could not complete the request.",
                        status_code=502,
                    )
                    status = "incomplete"
                    break
                if item is None:
                    status = "cancelled"
                    break
                if item == "finished":
                    break
                if item.type == "delta" and item.text:
                    parts.append(item.text)
                    yield _sse("delta", {"text": item.text})
                elif item.type == "end":
                    status = item.status
                    usage = item.usage
                    break
        finally:
            with contextlib.suppress(RuntimeError):
                await iterator.aclose()
            async with session_factory() as session:
                settings = request.app.state.settings
                model = (
                    settings.gemini_model
                    if settings.ai_provider == "gemini"
                    else settings.openai_model
                )
                await ChatService(session).finish(
                    prepared.assistant_message_id,
                    prepared.conversation_id,
                    "".join(parts),
                    status,
                    usage,
                    provider=settings.ai_provider,
                    model=model.strip() or "unconfigured",
                )
        if error is not None:
            yield _sse("error", {"code": error.code, "message": error.message})
        elif status != "cancelled":
            yield _sse(
                "done",
                {
                    "message_id": str(prepared.assistant_message_id),
                    "status": status,
                },
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    __: None = Depends(limit_chat),
) -> StreamingResponse:
    async with request.app.state.session_factory() as session:
        prepared = await ChatService(session, request.app.state.settings).prepare(
            user.id,
            body.conversation_id,
            body.content,
            body.file_ids,
            body.web_search,
        )
    return _stream(prepared, request, request.app.state.ai_provider)


@router.post("/chat/regenerate")
async def regenerate(
    body: RegenerateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    __: None = Depends(limit_chat),
) -> StreamingResponse:
    async with request.app.state.session_factory() as session:
        prepared = await ChatService(session, request.app.state.settings).prepare_regenerate(
            user.id,
            body.conversation_id,
            body.message_id,
        )
    return _stream(prepared, request, request.app.state.ai_provider)
