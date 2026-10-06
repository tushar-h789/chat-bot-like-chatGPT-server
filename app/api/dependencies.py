from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import CSRF_COOKIE, SESSION_COOKIE, tokens_match
from app.db.models.user import User
from app.services.auth import AuthService


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def limit_auth(request: Request) -> None:
    host = request.client.host if request.client else "unknown"
    request.app.state.rate_limiter.check(
        f"auth:{host}",
        request.app.state.settings.rate_limit_auth_per_minute,
    )


async def require_csrf(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("x-csrf-token", "")
    if not tokens_match(cookie, header):
        raise AppError(
            code="csrf_failed",
            message="CSRF token is missing or invalid.",
            status_code=403,
        )


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    if not raw_token:
        raise AppError(
            code="unauthenticated",
            message="Authentication is required.",
            status_code=401,
        )
    return await AuthService(db).authenticate(raw_token)


async def limit_chat(
    request: Request,
    user: User = Depends(get_current_user),
) -> None:
    request.app.state.rate_limiter.check(
        f"chat:{user.id}",
        request.app.state.settings.rate_limit_chat_per_minute,
    )
