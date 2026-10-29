"""QueryPlan (IR). Nothing in this module knows about SQL or the warehouse."""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TimeRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _check(self):
        if self.start >= self.end:
            raise ValueError("时间区间必须满足 start < end")
        return self


class FilterSpec(BaseModel):
    op: Literal["eq", "in", "neq", "gte", "lte", "gt", "lt"] = "eq"
    value: Any = None


class HavingSpec(BaseModel):
    metric: str
    op: Literal["gte", "lte", "gt", "lt", "eq"] = "gte"
    value: float


class OrderSpec(BaseModel):
    field: str = "value"
    direction: Literal["asc", "desc"] = "desc"


class CompareSpec(BaseModel):
    type: Literal["prev_period", "prev_year"] = "prev_period"


class ComparisonSpec(BaseModel):
    """Explicit period-vs-period comparison (LLM decides both ranges)."""
    label: str | None = None
    mode: str = "period_vs_period"
    current: TimeRange
    baseline: TimeRange


class NestedSpec(BaseModel):
    entity: str | None = None
    min_orders: int = 2


class QueryPlan(BaseModel):
    # Two plan shapes share one contract: aggregates need a metric, member
    # lists enumerate the values of one dimension and only need list_dim.
    kind: Literal["aggregate", "member_list", "coverage"] = "aggregate"
    plan_id: str = "p"
    metric: str | None = None
    metric_spec: dict | None = None
    metric_source: str | None = None
    list_dim: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    time: TimeRange | None = None
    time_axis: str | None = None
    filters: dict[str, FilterSpec] = Field(default_factory=dict)
    having: list[HavingSpec] = Field(default_factory=list)
    ratio_grain: str | None = None
    ratio_numerator: str | None = None
    ratio_denominator: str | None = None
    nested: NestedSpec | None = None
    order_by: list[OrderSpec] = Field(default_factory=list)
    limit: int = 100
    limit_per_group: int | None = None
    share: bool = False
    compare: CompareSpec | None = None
    comparison: ComparisonSpec | None = None
    notes: list[str] = Field(default_factory=list)
    visualization: dict | None = None
    coverage_period: Literal["month"] = "month"
    degraded_reason: str | None = None
    degraded_kind: str | None = None
    interpretation_id: str | None = None
    interpretation_label: str | None = None
    preferred: bool = False
