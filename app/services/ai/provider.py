from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

ChatRole = Literal["user", "assistant", "system"]


class ChatImage(BaseModel):
    mime_type: str
    data: bytes


class ChatDocument(BaseModel):
    name: str
    mime_type: str
    data: bytes


class ChatTurn(BaseModel):
    role: ChatRole
    content: str
    images: list[ChatImage] = []
    documents: list[ChatDocument] = []
    web_search: bool = False


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class ModelCompletion(BaseModel):
    response_id: str
    model: str
    text: str
    usage: TokenUsage | None


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, object] = {}
    result: str


class StreamItem(BaseModel):
    type: Literal["delta", "end", "tool"]
    text: str = ""
    status: Literal["complete", "incomplete"] = "complete"
    usage: TokenUsage | None = None
    tool: ToolCall | None = None


class AIProvider(Protocol):
    async def complete(self, turns: list[ChatTurn]) -> ModelCompletion:
        """Return one finished model response for the given turns."""

    def stream(self, turns: list[ChatTurn]) -> AsyncIterator[StreamItem]:
        """Yield text deltas, then one end item, from the provider stream."""
