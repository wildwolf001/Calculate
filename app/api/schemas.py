from __future__ import annotations

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    dataset_id: str | None = Field(default=None, max_length=64)


class CreateSessionResponse(BaseModel):
    session_id: str
    dataset_id: str


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class PublishRequest(BaseModel):
    metrics: list[str] | None = None
    dimensions: list[str] | None = None


class MetricEdit(BaseModel):
    id: str
    name_zh: str | None = None
    synonyms: list[str] | None = None
    unit: str | None = None
    definition: str | None = None


class DimensionEdit(BaseModel):
    id: str
    name_zh: str | None = None
    synonyms: list[str] | None = None


class DraftEditRequest(BaseModel):
    metrics: list[MetricEdit] | None = None
    dimensions: list[DimensionEdit] | None = None
    time_column: dict | None = None


class ClarificationRequest(BaseModel):
    value: str = Field(min_length=1, max_length=500)


class AnalysisRequest(BaseModel):
    sections: list[str] = Field(default_factory=list, max_length=12)
    custom_text: str | None = Field(default=None, max_length=500)
    custom_title: str | None = Field(default=None, max_length=60)


class ApiError(BaseModel):
    code: str
    message: str
