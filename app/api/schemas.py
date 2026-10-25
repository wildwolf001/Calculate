from __future__ import annotations

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)


class CreateSessionResponse(BaseModel):
    session_id: str


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ClarificationRequest(BaseModel):
    value: str = Field(min_length=1, max_length=500)


class ApiError(BaseModel):
    code: str
    message: str
