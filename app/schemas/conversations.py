from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings

TITLE_MAX_LENGTH = 200
DEFAULT_TITLE = "New chat"


def _clean_title(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Title cannot be blank.")
    if len(cleaned) > TITLE_MAX_LENGTH:
        raise ValueError("Title is too long.")
    return cleaned


class CreateConversation(BaseModel):
    title: str | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_title(value)


class RenameConversation(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _clean_title(value)


class CreateMessage(BaseModel):
    content: str = Field(max_length=100_000)

    @field_validator("content")
    @classmethod
    def content_within_limit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message content cannot be blank.")
        if len(value) > get_settings().max_message_chars:
            raise ValueError("Message content is too long.")
        return value


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    status: str
    created_at: datetime
    updated_at: datetime
