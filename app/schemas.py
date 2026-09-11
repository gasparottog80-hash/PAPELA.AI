from __future__ import annotations

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User prompt for the agent.")


class AgentResponse(BaseModel):
    request_id: str
    reply: str


class ErrorResponse(BaseModel):
    request_id: str
    error: str
