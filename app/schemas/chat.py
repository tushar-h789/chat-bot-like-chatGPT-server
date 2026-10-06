from uuid import UUID

from pydantic import BaseModel, field_validator

from app.core.config import get_settings


class ChatRequest(BaseModel):
    content: str
    conversation_id: UUID | None = None

    @field_validator("content")
    @classmethod
    def content_within_limit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message content cannot be blank.")
        if len(value) > get_settings().max_message_chars:
            raise ValueError("Message content is too long.")
        return value
