from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    media_type: str
    size_bytes: int
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: object) -> "FileResponse":
        return cls(
            id=stored.id,  # type: ignore[attr-defined]
            name=stored.original_name,  # type: ignore[attr-defined]
            media_type=stored.media_type,  # type: ignore[attr-defined]
            size_bytes=stored.size_bytes,  # type: ignore[attr-defined]
            created_at=stored.created_at,  # type: ignore[attr-defined]
        )
