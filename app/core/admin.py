from app.core.config import Settings
from app.db.models.user import User
from app.schemas.auth import UserResponse


def user_is_admin(user: User, settings: Settings) -> bool:
    return user.is_admin or user.email in settings.admin_email_set


def to_user_response(user: User, settings: Settings) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        created_at=user.created_at,
        is_admin=user_is_admin(user, settings),
    )
