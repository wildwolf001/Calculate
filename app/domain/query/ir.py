"""Typed query IR.

The parser still produces Slots; this module is the stable boundary between
language and planning. The planner keeps its legacy Slots entry point until the
model migration is complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


@dataclass(frozen=True)
class QueryIR:
    metric: str
    metric_expr: dict[str, Any] | None = None
    metric_source: str | None = None
    ratio_numerator: str | None = None
    ratio_denominator: str | None = None
    dimensions: tuple[str, ...] = ()
    filters: dict[str, Any] = field(default_factory=dict)
    time: dict[str, Any] | None = None
    time_axis: str | None = None
    compare: dict[str, Any] | None = None
    comparison: dict[str, Any] | None = None
    share: bool = False
    limit: int = 100
    limit_per_group: int | None = None
    order_by: tuple[dict[str, Any], ...] = ()
    having: tuple[dict[str, Any], ...] = ()
    nested: dict[str, Any] | None = None
    intent: str = "query"
    unresolved: tuple[str, ...] = ()
    source: str = "slots"

    @classmethod
    def from_slots(cls, slots: Any) -> "QueryIR":
        data = slots.model_dump(mode="json") if hasattr(slots, "model_dump") else dict(slots or {})
        return cls(
            metric=str(data.get("metric") or ""),
            metric_expr=_dump(data.get("metric_expr")),
            metric_source=(
                str(data["metric_source"]) if data.get("metric_source") else None
            ),
            ratio_numerator=(
                str(data["ratio_numerator"]) if data.get("ratio_numerator") else None
            ),
            ratio_denominator=(
                str(data["ratio_denominator"]) if data.get("ratio_denominator") else None
            ),
            dimensions=tuple(str(item) for item in (data.get("dimensions") or [])),
            filters={str(k): _dump(v) for k, v in (data.get("filters") or {}).items()},
            time=_dump(data.get("time")),
            time_axis=(str(data["time_axis"]) if data.get("time_axis") else None),
            compare=_dump(data.get("compare")),
            comparison=_dump(data.get("comparison")),
            share=bool(data.get("share", False)),
            limit=int(data.get("limit", 100)),
            limit_per_group=(
                int(data["limit_per_group"])
                if data.get("limit_per_group") is not None else None
            ),
            order_by=tuple(_dump(item) for item in (data.get("order_by") or [])),
            having=tuple(_dump(item) for item in (data.get("having") or [])),
            nested=_dump(data.get("nested")),
            intent=str(data.get("intent") or "query"),
            unresolved=tuple(str(item) for item in (data.get("unresolved") or [])),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "metric_expr": self.metric_expr,
            "metric_source": self.metric_source,
            "ratio_numerator": self.ratio_numerator,
            "ratio_denominator": self.ratio_denominator,
            "dimensions": list(self.dimensions),
            "filters": self.filters,
            "time": self.time,
            "time_axis": self.time_axis,
            "compare": self.compare,
            "comparison": self.comparison,
            "share": self.share,
            "limit": self.limit,
            "limit_per_group": self.limit_per_group,
            "order_by": list(self.order_by),
            "having": list(self.having),
            "nested": self.nested,
            "intent": self.intent,
            "unresolved": list(self.unresolved),
            "source": self.source,
        }

    def to_slots(self):
        """Lower the IR to the legacy Slots container for the planner."""
        from app.core.state import Slots

        data = self.as_dict()
        data["metric"] = self.metric or None
        data["dimensions"] = list(self.dimensions)
        data["filters"] = dict(self.filters)
        data["time"] = self.time
        data["compare"] = self.compare
        data["comparison"] = self.comparison
        data["order_by"] = list(self.order_by)
        data["having"] = list(self.having)
        data["nested"] = self.nested
        data["intent"] = self.intent
        data["unresolved"] = list(self.unresolved)
        return Slots.model_validate(data)
