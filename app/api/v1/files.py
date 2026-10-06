from uuid import UUID

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import FileResponse as Download
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, require_csrf
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models.user import User
from app.schemas.files import FileResponse
from app.services.files import FileService

router = APIRouter(prefix="/files", tags=["files"])


def _service(request: Request, db: AsyncSession) -> FileService:
    settings: Settings = request.app.state.settings
    return FileService(db, settings)


@router.post("", response_model=FileResponse, status_code=201)
async def upload_file(
    request: Request,
    file: UploadFile,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    data = await _read_limited(file, request.app.state.settings.max_upload_bytes)
    stored = await _service(request, db).save(user, file.filename, data)
    return FileResponse.from_stored(stored)


@router.get("", response_model=list[FileResponse])
async def list_files(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FileResponse]:
    rows = await _service(request, db).list_for_user(user)
    return [FileResponse.from_stored(row) for row in rows]


@router.get("/{file_id}", response_model=FileResponse)
async def get_file(
    file_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    stored = await _service(request, db).get_for_user(user, file_id)
    return FileResponse.from_stored(stored)


@router.get("/{file_id}/content")
async def download_file(
    file_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Download:
    service = _service(request, db)
    stored = await service.get_for_user(user, file_id)
    return Download(
        service.path_for(stored),
        media_type=stored.media_type,
        filename=stored.original_name,
    )


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    file_id: UUID,
    request: Request,
    user: User = Depends(get_current_user),
    _: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _service(request, db).delete(user, file_id)


async def _read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise AppError(
                code="payload_too_large",
                message="The file is too large.",
                status_code=413,
            )
        chunks.append(chunk)
    return b"".join(chunks)
