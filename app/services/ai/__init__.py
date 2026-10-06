"""Model provider seam. Each provider module imports its own SDK."""

from app.core.config import Settings
from app.services.ai.gemini_provider import GeminiProvider
from app.services.ai.openai_provider import OpenAIProvider
from app.services.ai.provider import (
    AIProvider,
    ChatTurn,
    ModelCompletion,
    StreamItem,
    TokenUsage,
)

__all__ = [
    "AIProvider",
    "ChatTurn",
    "GeminiProvider",
    "ModelCompletion",
    "OpenAIProvider",
    "StreamItem",
    "TokenUsage",
    "build_provider",
]


def build_provider(settings: Settings) -> AIProvider:
    """Select the chat provider from AI_PROVIDER. Gemini is the default."""
    if settings.ai_provider == "openai":
        return OpenAIProvider(settings)
    return GeminiProvider(settings)
