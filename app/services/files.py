import re
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models.stored_file import StoredFile
from app.db.models.user import User

IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
    }
)
TEXT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }
)
PDF_MEDIA_TYPES = frozenset({"application/pdf"})
ALLOWED_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\\/]+")


def read_text_excerpt(settings: Settings, stored: StoredFile) -> str | None:
    """Return UTF-8 text for a text file, trimmed to the message character limit."""
    if stored.media_type not in TEXT_MEDIA_TYPES:
        return None
    path = (settings.upload_dir / str(stored.user_id) / stored.storage_name).resolve()
    root = settings.upload_dir.resolve()
    if root not in path.parents or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    limit = settings.max_message_chars
    if len(text) > limit:
        return text[:limit].rstrip() + "\n[truncated]"
    return text


def read_pdf_bytes(settings: Settings, stored: StoredFile) -> bytes | None:
    """Return PDF bytes for a PDF. Other types stay unread."""
    if stored.media_type not in PDF_MEDIA_TYPES:
        return None
    return _read_stored_bytes(settings, stored)


def read_image_bytes(settings: Settings, stored: StoredFile) -> bytes | None:
    """Return image bytes for a supported image. Other types stay unread."""
    if stored.media_type not in IMAGE_MEDIA_TYPES:
        return None
    return _read_stored_bytes(settings, stored)


def _read_stored_bytes(settings: Settings, stored: StoredFile) -> bytes | None:
    path = (settings.upload_dir / str(stored.user_id) / stored.storage_name).resolve()
    root = settings.upload_dir.resolve()
    if root not in path.parents or not path.is_file():
        return None
    data = path.read_bytes()
    if not data:
        return None
    return data


def display_name(raw_name: str | None) -> tuple[str, str]:
    """Return a safe display name and its allowed extension."""
    base = Path(raw_name or "file").name
    base = _UNSAFE_NAME.sub("", base).strip().strip(".")
    if not base:
        base = "file"
    suffix = Path(base).suffix.lower()
    media_type = ALLOWED_TYPES.get(suffix)
    if media_type is None:
        raise AppError(
            code="unsupported_file",
            message="This file type is not supported.",
            status_code=422,
        )
    if len(base) > 200:
        stem = Path(base).stem[: 200 - len(suffix)]
        base = f"{stem}{suffix}"
    return base, media_type


class FileService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def save(self, user: User, raw_name: str | None, data: bytes) -> StoredFile:
        if not data:
            raise AppError(
                code="validation_error",
                message="The file is empty.",
                status_code=422,
            )
        if len(data) > self._settings.max_upload_bytes:
            raise AppError(
                code="payload_too_large",
                message="The file is too large.",
                status_code=413,
            )
        name, media_type = display_name(raw_name)
        suffix = Path(name).suffix.lower()
        storage_name = f"{uuid4().hex}{suffix}"
        directory = self._user_dir(user.id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / storage_name
        target.write_bytes(data)
        stored = StoredFile(
            user_id=user.id,
            original_name=name,
            media_type=media_type,
            size_bytes=len(data),
            storage_name=storage_name,
        )
        self._session.add(stored)
        await self._session.commit()
        await self._session.refresh(stored)
        return stored

    async def list_for_user(self, user: User) -> list[StoredFile]:
        rows = await self._session.scalars(
            select(StoredFile)
            .where(StoredFile.user_id == user.id)
            .order_by(StoredFile.created_at.desc(), StoredFile.id.desc())
        )
        return list(rows)

    async def get_for_user(self, user: User, file_id: UUID) -> StoredFile:
        stored = await self._session.get(StoredFile, file_id)
        if stored is None or stored.user_id != user.id:
            raise _missing()
        return stored

    def path_for(self, stored: StoredFile) -> Path:
        path = (self._user_dir(stored.user_id) / stored.storage_name).resolve()
        root = self._settings.upload_dir.resolve()
        if root not in path.parents:
            raise _missing()
        return path

    async def delete(self, user: User, file_id: UUID) -> None:
        stored = await self.get_for_user(user, file_id)
        path = self.path_for(stored)
        await self._session.delete(stored)
        await self._session.commit()
        path.unlink(missing_ok=True)

    def _user_dir(self, user_id: UUID) -> Path:
        return self._settings.upload_dir / str(user_id)


def _missing() -> AppError:
    return AppError(
        code="file_not_found",
        message="The file was not found.",
        status_code=404,
    )
