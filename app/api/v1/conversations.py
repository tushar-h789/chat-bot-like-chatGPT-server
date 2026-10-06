from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, require_csrf
from app.db.models.conversation import Conversation
from app.db.models.user import User
from app.schemas.conversations import (
    ConversationResponse,
    CreateConversation,
    RenameConversation,
)
from app.services.conversations import ConversationService

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    body: CreateConversation,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    return await ConversationService(db).create(user, body.title)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Conversation]:
    return await ConversationService(db).list_for_user(user)


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    return await ConversationService(db).get(user, conversation_id)


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def rename_conversation(
    conversation_id: UUID,
    body: RenameConversation,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    return await ConversationService(db).rename(user, conversation_id, body.title)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await ConversationService(db).delete(user, conversation_id)
    return Response(status_code=204)
