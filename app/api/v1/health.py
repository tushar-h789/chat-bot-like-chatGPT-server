import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.errors import AppError

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
async def health(db: AsyncSession = Depends(get_db)) -> HealthResponse:
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("database health check failed: %s", type(exc).__name__)
        raise AppError(
            code="database_unavailable",
            message="Database is unavailable.",
            status_code=503,
        ) from exc
    return HealthResponse(status="ok")
