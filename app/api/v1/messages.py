from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, require_csrf
from app.db.models.message import Message
from app.db.models.user import User
from app.schemas.conversations import CreateMessage, MessageResponse
from app.services.conversations import ConversationService

router = APIRouter(
    prefix="/conversations/{conversation_id}/messages",
    tags=["messages"],
)


@router.post("", response_model=MessageResponse, status_code=201)
async def create_message(
    conversation_id: UUID,
    body: CreateMessage,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Message:
    return await ConversationService(db).add_message(user, conversation_id, body.content)


@router.get("", response_model=list[MessageResponse])
async def list_messages(
    conversation_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    return await ConversationService(db).list_messages(user, conversation_id)
