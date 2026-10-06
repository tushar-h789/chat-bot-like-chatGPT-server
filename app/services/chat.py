from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import utcnow
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.models.stored_file import StoredFile
from app.db.models.usage_event import UsageEvent
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.schemas.conversations import DEFAULT_TITLE, TITLE_MAX_LENGTH, files_from_parts
from app.services.files import TEXT_MEDIA_TYPES, read_text_excerpt
from app.services.ai.provider import ChatTurn, TokenUsage
from app.services.conversations import _not_found


@dataclass(frozen=True)
class PreparedChat:
    conversation_id: UUID
    title: str
    assistant_message_id: UUID
    turns: list[ChatTurn]


def title_from_message(content: str) -> str:
    line = content.strip().splitlines()[0].strip()
    if len(line) <= TITLE_MAX_LENGTH:
        return line
    return line[:TITLE_MAX_LENGTH].rstrip()


class ChatService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    async def prepare(
        self,
        user_id: UUID,
        conversation_id: UUID | None,
        content: str,
        file_ids: list[UUID] | None = None,
    ) -> PreparedChat:
        if conversation_id is None:
            conversation = Conversation(
                user_id=user_id, title=title_from_message(content)
            )
            self._conversations.add(conversation)
            await self._session.flush()
            prior: list[Message] = []
        else:
            conversation = await self._conversations.get_for_user(
                conversation_id, user_id
            )
            if conversation is None:
                raise _not_found()
            prior = await self._messages.list_for_conversation(conversation.id)
            if conversation.title == DEFAULT_TITLE and not any(
                message.role == "user" for message in prior
            ):
                conversation.title = title_from_message(content)

        started = utcnow()
        attached = await self._owned_files(user_id, file_ids or [])
        user_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=content,
            content_parts=_file_parts(attached) or None,
            status="complete",
            created_at=started,
            updated_at=started,
        )
        self._messages.add(user_message)
        await self._session.flush()
        turns = await self._model_turns([*prior, user_message])
        assistant_at = started + timedelta(microseconds=1)
        assistant = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="incomplete",
            created_at=assistant_at,
            updated_at=assistant_at,
        )
        self._messages.add(assistant)
        conversation.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(assistant)
        return PreparedChat(
            conversation_id=conversation.id,
            title=conversation.title,
            assistant_message_id=assistant.id,
            turns=turns,
        )

    async def prepare_regenerate(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
    ) -> PreparedChat:
        conversation = await self._conversations.get_for_user(conversation_id, user_id)
        if conversation is None:
            raise _not_found()
        messages = await self._messages.list_for_conversation(conversation.id)
        target = next(
            (message for message in messages if message.id == message_id),
            None,
        )
        if target is None or target.role != "assistant":
            raise _not_found()
        if messages[-1].id != target.id:
            raise AppError(
                code="not_regenerable",
                message="Only the latest reply can be regenerated.",
                status_code=422,
            )
        earlier = messages[: messages.index(target)]
        has_user_turn = any(
            message.role == "user" and message.content.strip() for message in earlier
        )
        if not has_user_turn:
            raise AppError(
                code="not_regenerable",
                message="Only the latest reply can be regenerated.",
                status_code=422,
            )
        turns = await self._model_turns(earlier)
        target.content = ""
        target.status = "incomplete"
        target.metadata_ = None
        target.updated_at = utcnow()
        await self._session.execute(
            delete(UsageEvent).where(UsageEvent.message_id == target.id)
        )
        conversation.updated_at = utcnow()
        await self._session.commit()
        return PreparedChat(
            conversation_id=conversation.id,
            title=conversation.title,
            assistant_message_id=target.id,
            turns=turns,
        )

    async def finish(
        self,
        assistant_message_id: UUID,
        conversation_id: UUID,
        content: str,
        status: str,
        usage: TokenUsage | None,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        assistant = await self._session.get(Message, assistant_message_id)
        conversation = await self._session.get(Conversation, conversation_id)
        if assistant is None or conversation is None:
            return
        assistant.content = content
        assistant.status = status
        if usage is not None:
            assistant.metadata_ = {"usage": usage.model_dump()}
            if provider and model:
                self._session.add(
                    UsageEvent(
                        user_id=conversation.user_id,
                        message_id=assistant.id,
                        provider=provider,
                        model=model[:100],
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                    )
                )
        conversation.updated_at = utcnow()
        await self._session.commit()

    async def _owned_files(
        self, user_id: UUID, file_ids: list[UUID]
    ) -> list[StoredFile]:
        unique = list(dict.fromkeys(file_ids))
        if not unique:
            return []
        rows = await self._session.scalars(
            select(StoredFile).where(
                StoredFile.id.in_(unique),
                StoredFile.user_id == user_id,
            )
        )
        found = {row.id: row for row in rows}
        if len(found) != len(unique):
            raise AppError(
                code="file_not_found",
                message="The file was not found.",
                status_code=404,
            )
        return [found[file_id] for file_id in unique]

    async def _model_turns(self, messages: list[Message]) -> list[ChatTurn]:
        excerpts = await self._text_excerpts(messages)
        turns: list[ChatTurn] = []
        for message in messages:
            if message.role not in {"user", "assistant", "system"}:
                continue
            content = _with_attached_text(message, excerpts)
            if content.strip():
                turns.append(
                    ChatTurn(role=message.role, content=content)  # type: ignore[arg-type]
                )
        return turns

    async def _text_excerpts(self, messages: list[Message]) -> dict[UUID, str]:
        if self._settings is None:
            return {}
        file_ids = [
            attached.id
            for message in messages
            for attached in files_from_parts(message.content_parts)
            if attached.media_type in TEXT_MEDIA_TYPES
        ]
        if not file_ids:
            return {}
        rows = await self._session.scalars(
            select(StoredFile).where(StoredFile.id.in_(file_ids))
        )
        excerpts: dict[UUID, str] = {}
        for stored in rows:
            text = read_text_excerpt(self._settings, stored)
            if text is not None:
                excerpts[stored.id] = text
        return excerpts


def _with_attached_text(message: Message, excerpts: dict[UUID, str]) -> str:
    blocks = [message.content]
    for attached in files_from_parts(message.content_parts):
        if attached.media_type not in TEXT_MEDIA_TYPES:
            continue
        text = excerpts.get(attached.id)
        if text is None:
            blocks.append(f"[Attached file {attached.name} could not be read.]")
        else:
            blocks.append(f"[Attached file {attached.name}]\n{text}")
    return "\n\n".join(block for block in blocks if block)


def _file_parts(files: list[StoredFile]) -> list[dict[str, object]]:
    return [
        {
            "type": "file",
            "file_id": str(item.id),
            "name": item.original_name,
            "media_type": item.media_type,
            "size_bytes": item.size_bytes,
        }
        for item in files
    ]
