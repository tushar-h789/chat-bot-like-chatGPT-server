import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Response
from pwdlib import PasswordHash

from app.core.config import Settings

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"
SESSION_TTL = timedelta(days=7)

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


def dummy_password_hash() -> str:
    """A stable hash used when login cannot find the user.

    Verifying against it keeps the response time close to a real check.
    """
    return _DUMMY_PASSWORD_HASH


_DUMMY_PASSWORD_HASH = hash_password("not-a-real-user-password")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_bytes = left.encode("utf-8")
    right_bytes = right.encode("utf-8")
    if len(left_bytes) != len(right_bytes):
        return False
    return secrets.compare_digest(left_bytes, right_bytes)


def utcnow() -> datetime:
    return datetime.now(UTC)


def session_expires_at() -> datetime:
    return utcnow() + SESSION_TTL


def _secure(settings: Settings) -> bool:
    return settings.environment == "production"


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=_secure(settings),
        samesite="lax",
        path="/",
        max_age=int(SESSION_TTL.total_seconds()),
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value="",
        httponly=True,
        secure=_secure(settings),
        samesite="lax",
        path="/",
        max_age=0,
    )


def set_csrf_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        httponly=False,
        secure=_secure(settings),
        samesite="lax",
        path="/",
        max_age=int(SESSION_TTL.total_seconds()),
    )
