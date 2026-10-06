from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import (
    dummy_password_hash,
    hash_password,
    hash_token,
    new_token,
    session_expires_at,
    utcnow,
    verify_password,
)
from app.db.models.auth_session import AuthSession
from app.db.models.user import User
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository

def _invalid_login() -> AppError:
    return AppError(
        code="invalid_credentials",
        message="Invalid email or password.",
        status_code=401,
    )


def _unauthenticated() -> AppError:
    return AppError(
        code="unauthenticated",
        message="Authentication is required.",
        status_code=401,
    )


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._sessions = SessionRepository(session)

    async def register(self, email: str, password: str) -> tuple[User, str]:
        user = User(email=normalize_email(email), password_hash=hash_password(password))
        self._users.add(user)
        try:
            await self._session.flush()
        except IntegrityError:
            await self._session.rollback()
            raise AppError(
                code="email_already_registered",
                message="An account with this email already exists.",
                status_code=409,
            ) from None

        raw_token = await self._create_session(user)
        await self._session.commit()
        await self._session.refresh(user)
        return user, raw_token

    async def login(self, email: str, password: str) -> tuple[User, str]:
        user = await self._users.get_by_email(normalize_email(email))
        password_hash = user.password_hash if user is not None else dummy_password_hash()
        if user is None or not verify_password(password, password_hash):
            raise _invalid_login()

        raw_token = await self._create_session(user)
        await self._session.commit()
        await self._session.refresh(user)
        return user, raw_token

    async def authenticate(self, raw_token: str) -> User:
        auth_session = await self._sessions.get_active_by_token_hash(
            hash_token(raw_token),
            utcnow(),
        )
        if auth_session is None:
            raise _unauthenticated()
        user = await self._users.get_by_id(auth_session.user_id)
        if user is None:
            raise _unauthenticated()
        return user

    async def logout(self, raw_token: str) -> None:
        auth_session = await self._sessions.get_by_token_hash(hash_token(raw_token))
        if auth_session is None or auth_session.revoked_at is not None:
            raise _unauthenticated()
        auth_session.revoked_at = utcnow()
        await self._session.commit()

    async def _create_session(self, user: User) -> str:
        raw_token = new_token()
        self._sessions.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=session_expires_at(),
            )
        )
        return raw_token
