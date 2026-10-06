from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.auth_session import AuthSession


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, auth_session: AuthSession) -> None:
        self._session.add(auth_session)

    async def get_active_by_token_hash(
        self,
        token_hash: str,
        now: datetime,
    ) -> AuthSession | None:
        result = await self._session.execute(
            select(AuthSession).where(
                AuthSession.token_hash == token_hash,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_token_hash(self, token_hash: str) -> AuthSession | None:
        result = await self._session.execute(
            select(AuthSession).where(AuthSession.token_hash == token_hash)
        )
        return result.scalar_one_or_none()
