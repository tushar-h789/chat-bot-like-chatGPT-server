from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import utcnow
from app.db.models.conversation import Conversation
from app.db.models.message import Message
from app.db.models.user import User
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.schemas.conversations import DEFAULT_TITLE


def _not_found() -> AppError:
    return AppError(
        code="not_found",
        message="Conversation not found.",
        status_code=404,
    )


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    async def create(self, user: User, title: str | None) -> Conversation:
        conversation = Conversation(user_id=user.id, title=title or DEFAULT_TITLE)
        self._conversations.add(conversation)
        await self._session.commit()
        await self._session.refresh(conversation)
        return conversation

    async def list_for_user(self, user: User) -> list[Conversation]:
        return await self._conversations.list_for_user(user.id)

    async def get(self, user: User, conversation_id: UUID) -> Conversation:
        conversation = await self._conversations.get_for_user(conversation_id, user.id)
        if conversation is None:
            raise _not_found()
        return conversation

    async def rename(
        self,
        user: User,
        conversation_id: UUID,
        title: str,
    ) -> Conversation:
        conversation = await self.get(user, conversation_id)
        conversation.title = title
        conversation.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(conversation)
        return conversation

    async def delete(self, user: User, conversation_id: UUID) -> None:
        conversation = await self.get(user, conversation_id)
        await self._conversations.delete(conversation)
        await self._session.commit()

    async def add_message(
        self,
        user: User,
        conversation_id: UUID,
        content: str,
    ) -> Message:
        conversation = await self.get(user, conversation_id)
        message = Message(
            conversation_id=conversation.id,
            role="user",
            content=content,
            status="complete",
        )
        self._messages.add(message)
        conversation.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(message)
        return message

    async def list_messages(self, user: User, conversation_id: UUID) -> list[Message]:
        conversation = await self.get(user, conversation_id)
        return await self._messages.list_for_conversation(conversation.id)
