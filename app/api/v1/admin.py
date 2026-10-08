from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_admin, get_db
from app.core.config import Settings
from app.db.models.user import User
from app.schemas.admin import AdminStatsResponse
from app.services.admin import AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats", response_model=AdminStatsResponse)
async def admin_stats(
    request: Request,
    _: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminStatsResponse:
    settings: Settings = request.app.state.settings
    return await AdminService(db, settings).stats()
