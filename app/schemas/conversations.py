from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class AttachedFile(BaseModel):
    id: UUID
    name: str
    media_type: str
    size_bytes: int


def _used_web_search(metadata: object) -> bool:
    return isinstance(metadata, dict) and metadata.get("web_search") is True


def files_from_parts(parts: object) -> list[AttachedFile]:
    if not isinstance(parts, list):
        return []
    files: list[AttachedFile] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "file":
            continue
        try:
            files.append(
                AttachedFile(
                    id=part["file_id"],
                    name=str(part["name"]),
                    media_type=str(part["media_type"]),
                    size_bytes=int(part["size_bytes"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return files


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    status: str
    created_at: datetime
    updated_at: datetime
    files: list[AttachedFile] = []
    web_search: bool = False

    @model_validator(mode="before")
    @classmethod
    def include_saved_files(cls, value: object) -> object:
        if isinstance(value, dict):
            return value
        if not hasattr(value, "content"):
            return value
        return {
            "id": value.id,
            "conversation_id": value.conversation_id,
            "role": value.role,
            "content": value.content,
            "status": value.status,
            "created_at": value.created_at,
            "updated_at": value.updated_at,
            "files": files_from_parts(getattr(value, "content_parts", None)),
            "web_search": _used_web_search(getattr(value, "metadata_", None)),
        }
