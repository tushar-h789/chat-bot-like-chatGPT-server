from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin import user_is_admin
from app.core.config import Settings
from app.core.security import utcnow
from app.db.models.auth_session import AuthSession
from app.db.models.usage_event import UsageEvent
from app.db.models.user import User
from app.schemas.admin import AdminStatsResponse, AdminUserRow
from app.services.usage import utc_day_start


class AdminService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def stats(self) -> AdminStatsResponse:
        now = utcnow()
        total_users = int(
            await self._session.scalar(select(func.count()).select_from(User)) or 0
        )
        active_sessions = int(
            await self._session.scalar(
                select(func.count()).select_from(AuthSession).where(
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
            )
            or 0
        )
        users_with_usage = int(
            await self._session.scalar(
                select(func.count(func.distinct(UsageEvent.user_id)))
            )
            or 0
        )
        usage = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                    func.count(),
                )
            )
        ).one()
        tokens_today = int(
            await self._session.scalar(
                select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)).where(
                    UsageEvent.created_at >= utc_day_start()
                )
            )
            or 0
        )
        usage_counts = (
            select(
                UsageEvent.user_id.label("user_id"),
                func.count().label("replies"),
                func.coalesce(func.sum(UsageEvent.total_tokens), 0).label(
                    "total_tokens"
                ),
            )
            .group_by(UsageEvent.user_id)
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(
                    User,
                    func.coalesce(usage_counts.c.replies, 0),
                    func.coalesce(usage_counts.c.total_tokens, 0),
                )
                .outerjoin(usage_counts, User.id == usage_counts.c.user_id)
                .order_by(User.created_at.desc())
                .limit(200)
            )
        ).all()
        return AdminStatsResponse(
            total_users=total_users,
            active_sessions=active_sessions,
            users_with_usage=users_with_usage,
            input_tokens=int(usage[0]),
            output_tokens=int(usage[1]),
            total_tokens=int(usage[2]),
            replies=int(usage[3]),
            tokens_today=tokens_today,
            users=[
                AdminUserRow(
                    id=user.id,
                    email=user.email,
                    created_at=user.created_at,
                    is_admin=user_is_admin(user, self._settings),
                    replies=int(replies),
                    total_tokens=int(total_tokens),
                )
                for user, replies, total_tokens in rows
            ],
        )
