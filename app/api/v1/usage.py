from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db
from app.core.config import Settings
from app.db.models.user import User
from app.schemas.usage import UsageResponse
from app.services.usage import UsageService

router = APIRouter(tags=["usage"])


@router.get("/usage", response_model=UsageResponse)
async def usage(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UsageResponse:
    settings: Settings = request.app.state.settings
    return await UsageService(db, settings).summary(user.id)
