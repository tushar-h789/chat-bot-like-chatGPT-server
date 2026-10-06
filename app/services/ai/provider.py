from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

ChatRole = Literal["user", "assistant", "system"]


class ChatTurn(BaseModel):
    role: ChatRole
    content: str


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ModelCompletion(BaseModel):
    response_id: str
    model: str
    text: str
    usage: TokenUsage | None


class StreamItem(BaseModel):
    type: Literal["delta", "end"]
    text: str = ""
    status: Literal["complete", "incomplete"] = "complete"
    usage: TokenUsage | None = None


class AIProvider(Protocol):
    async def complete(self, turns: list[ChatTurn]) -> ModelCompletion:
        """Return one finished model response for the given turns."""

    def stream(self, turns: list[ChatTurn]) -> AsyncIterator[StreamItem]:
        """Yield text deltas, then one end item, from the provider stream."""
