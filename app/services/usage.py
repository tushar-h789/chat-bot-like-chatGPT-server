from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models.usage_event import UsageEvent
from app.schemas.usage import UsageResponse


class UsageService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def summary(self, user_id: UUID) -> UsageResponse:
        rows = (
            await self._session.execute(
                select(
                    UsageEvent.provider,
                    func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                    func.coalesce(func.sum(UsageEvent.total_tokens), 0),
                    func.count(),
                )
                .where(UsageEvent.user_id == user_id)
                .group_by(UsageEvent.provider)
            )
        ).all()
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        replies = 0
        cost = Decimal(0)
        priced = True
        for provider, provider_input, provider_output, provider_total, provider_replies in rows:
            input_tokens += int(provider_input)
            output_tokens += int(provider_output)
            total_tokens += int(provider_total)
            replies += int(provider_replies)
            provider_cost = _provider_cost(
                self._settings,
                str(provider),
                int(provider_input),
                int(provider_output),
            )
            if provider_cost is None:
                priced = False
            else:
                cost += provider_cost
        if not rows:
            cost_usd = "0.000000"
        elif priced:
            cost_usd = f"{cost:.6f}"
        else:
            cost_usd = None
        limit = self._settings.usage_limit_tokens_per_day
        return UsageResponse(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            replies=replies,
            cost_usd=cost_usd,
            tokens_today=await self.tokens_since(user_id, utc_day_start()),
            daily_token_limit=limit if limit > 0 else None,
        )

    async def enforce_daily(self, user_id: UUID) -> None:
        """Refuse a new model call once today's finished replies reach the cap."""
        limit = self._settings.usage_limit_tokens_per_day
        if limit <= 0:
            return
        used = await self.tokens_since(user_id, utc_day_start())
        if used >= limit:
            raise AppError(
                code="usage_limited",
                message="The daily usage limit has been reached.",
                status_code=429,
            )

    async def tokens_since(self, user_id: UUID, start: datetime) -> int:
        used = await self._session.scalar(
            select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)).where(
                UsageEvent.user_id == user_id,
                UsageEvent.created_at >= start,
            )
        )
        return int(used or 0)


def utc_day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _provider_cost(
    settings: Settings,
    provider: str,
    input_tokens: int,
    output_tokens: int,
) -> Decimal | None:
    if provider == "gemini":
        input_price = settings.gemini_input_usd_per_million
        output_price = settings.gemini_output_usd_per_million
    elif provider == "openai":
        input_price = settings.openai_input_usd_per_million
        output_price = settings.openai_output_usd_per_million
    else:
        return None
    if input_price is None or output_price is None:
        return None
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * Decimal(str(input_price))
        + Decimal(output_tokens) * Decimal(str(output_price))
    ) / million
