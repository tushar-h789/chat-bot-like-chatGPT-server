from uuid import UUID

from pydantic import BaseModel, field_validator

from app.core.config import get_settings


class RegenerateRequest(BaseModel):
    conversation_id: UUID
    message_id: UUID


class ChatRequest(BaseModel):
    content: str
    conversation_id: UUID | None = None
    file_ids: list[UUID] = []
    web_search: bool = False

    @field_validator("file_ids")
    @classmethod
    def limit_files(cls, value: list[UUID]) -> list[UUID]:
        if len(value) > 4:
            raise ValueError("A message can include at most 4 files.")
        return value

    @field_validator("content")
    @classmethod
    def content_within_limit(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message content cannot be blank.")
        if len(value) > get_settings().max_message_chars:
            raise ValueError("Message content is too long.")
        return value
