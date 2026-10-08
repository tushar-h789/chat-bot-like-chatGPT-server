from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr


class AdminUserRow(BaseModel):
    id: UUID
    email: EmailStr
    created_at: datetime
    is_admin: bool
    replies: int
    total_tokens: int


class AdminStatsResponse(BaseModel):
    total_users: int
    active_sessions: int
    users_with_usage: int
    replies: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tokens_today: int
    users: list[AdminUserRow]
