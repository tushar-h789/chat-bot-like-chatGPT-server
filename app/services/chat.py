from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.schemas.conversations import DEFAULT_TITLE, TITLE_MAX_LENGTH
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
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    async def prepare(
        self,
        user_id: UUID,
        conversation_id: UUID | None,
        content: str,
    ) -> PreparedChat:
        if conversation_id is None:
            conversation = Conversation(user_id=user_id, title=title_from_message(content))
            self._conversations.add(conversation)
            await self._session.flush()
            prior: list[Message] = []
        else:
            conversation = await self._conversations.get_for_user(conversation_id, user_id)
            if conversation is None:
                raise _not_found()
            prior = await self._messages.list_for_conversation(conversation.id)
            if conversation.title == DEFAULT_TITLE and not any(
                message.role == "user" for message in prior
            ):
                conversation.title = title_from_message(content)

        started = utcnow()
        user_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=content,
            status="complete",
            created_at=started,
            updated_at=started,
        )
        self._messages.add(user_message)
        await self._session.flush()
        turns = [
            ChatTurn(role=message.role, content=message.content)  # type: ignore[arg-type]
            for message in [*prior, user_message]
            if message.role in {"user", "assistant", "system"} and message.content.strip()
        ]
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

    async def finish(
        self,
        assistant_message_id: UUID,
        conversation_id: UUID,
        content: str,
        status: str,
        usage: TokenUsage | None,
    ) -> None:
        assistant = await self._session.get(Message, assistant_message_id)
        conversation = await self._session.get(Conversation, conversation_id)
        if assistant is None or conversation is None:
            return
        assistant.content = content
        assistant.status = status
        if usage is not None:
            assistant.metadata_ = {"usage": usage.model_dump()}
        conversation.updated_at = utcnow()
        await self._session.commit()
