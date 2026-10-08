from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user, get_db, limit_auth, require_csrf
from app.core.config import Settings
from app.core.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    clear_session_cookie,
    new_token,
    set_csrf_cookie,
    set_session_cookie,
)
from app.core.admin import to_user_response
from app.db.models.user import User
from app.schemas.auth import Credentials, CsrfResponse, UserResponse
from app.services.auth import AuthService, normalize_email

router = APIRouter(prefix="/auth", tags=["auth"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/csrf", response_model=CsrfResponse)
async def issue_csrf(request: Request, response: Response) -> CsrfResponse:
    token = request.cookies.get(CSRF_COOKIE) or new_token()
    set_csrf_cookie(response, token, _settings(request))
    return CsrfResponse(csrf_token=token)


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(
    credentials: Credentials,
    request: Request,
    response: Response,
    _: None = Depends(limit_auth),
    __: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    settings = _settings(request)
    email = normalize_email(credentials.email)
    user, token = await AuthService(db).register(
        credentials.email,
        credentials.password,
        grant_admin=email in settings.admin_email_set,
    )
    set_session_cookie(response, token, settings)
    return to_user_response(user, settings)


@router.post("/login", response_model=UserResponse)
async def login(
    credentials: Credentials,
    request: Request,
    response: Response,
    _: None = Depends(limit_auth),
    __: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    settings = _settings(request)
    email = normalize_email(credentials.email)
    user, token = await AuthService(db).login(
        credentials.email,
        credentials.password,
        grant_admin=email in settings.admin_email_set,
    )
    set_session_cookie(response, token, settings)
    return to_user_response(user, settings)


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    _: User = Depends(get_current_user),
    __: None = Depends(require_csrf),
    db: AsyncSession = Depends(get_db),
) -> None:
    raw_token = request.cookies.get(SESSION_COOKIE, "")
    await AuthService(db).logout(raw_token)
    clear_session_cookie(response, _settings(request))


@router.get("/me", response_model=UserResponse)
async def current_user(
    request: Request,
    user: User = Depends(get_current_user),
) -> UserResponse:
    return to_user_response(user, _settings(request))
