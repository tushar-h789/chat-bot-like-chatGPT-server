from pydantic import BaseModel


class UsageResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    replies: int
    cost_usd: str | None
