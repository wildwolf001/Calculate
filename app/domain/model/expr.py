"""Typed metric expressions.

The compiler still consumes the legacy string expressions; this AST is the
contract for the model layer and for the next compiler step.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

AGG_RE = re.compile(r"^\s*(SUM|AVG|MIN|MAX|COUNT)\s*\((.*)\)\s*$", re.I | re.S)
DISTINCT_RE = re.compile(r"^\s*DISTINCT\s+(.+)$", re.I | re.S)


@dataclass(frozen=True)
class MetricExpr:
    def as_dict(self) -> dict:
        return {"kind": type(self).__name__}


@dataclass(frozen=True)
class RawExpr(MetricExpr):
    expr: str
    metric_type: str = "aggregate"

    def as_dict(self) -> dict:
        return {"kind": "raw", "expr": self.expr, "metric_type": self.metric_type}


@dataclass(frozen=True)
class AggExpr(MetricExpr):
    aggregation: str
    operand: str

    def as_dict(self) -> dict:
        return {
            "kind": "agg",
            "aggregation": self.aggregation,
            "operand": self.operand,
        }


@dataclass(frozen=True)
class CountDistinctExpr(MetricExpr):
    operand: str

    def as_dict(self) -> dict:
        return {"kind": "count_distinct", "operand": self.operand}


@dataclass(frozen=True)
class RatioExpr(MetricExpr):
    numerator: MetricExpr
    denominator: MetricExpr

    def as_dict(self) -> dict:
        return {
            "kind": "ratio",
            "numerator": self.numerator.as_dict(),
            "denominator": self.denominator.as_dict(),
        }


@dataclass(frozen=True)
class FilteredExpr(MetricExpr):
    metric: MetricExpr
    predicate: str

    def as_dict(self) -> dict:
        return {
            "kind": "filtered",
            "metric": self.metric.as_dict(),
            "predicate": self.predicate,
        }


@dataclass(frozen=True)
class EntityCountExpr(MetricExpr):
    entity: str
    count_orders: str | None = None
    min_orders: int = 2

    def as_dict(self) -> dict:
        return {
            "kind": "entity_count",
            "entity": self.entity,
            "count_orders": self.count_orders,
            "min_orders": self.min_orders,
        }


def parse_expression(expr: str) -> MetricExpr:
    text = str(expr or "").strip()
    if not text:
        return RawExpr("")
    match = AGG_RE.match(text)
    if not match:
        return RawExpr(text)
    aggregation = match.group(1).upper()
    inner = match.group(2).strip()
    distinct = DISTINCT_RE.match(inner)
    if distinct:
        return CountDistinctExpr(distinct.group(1).strip())
    return AggExpr(aggregation, inner)


def from_metric_raw(raw: dict[str, Any]) -> MetricExpr:
    metric_type = str(raw.get("type") or "aggregate")
    if metric_type == "ratio":
        numerator = raw.get("numerator") or raw.get("grain_numerator") or ""
        denominator = raw.get("denominator") or raw.get("grain_denominator") or ""
        return RatioExpr(parse_expression(str(numerator)),
                         parse_expression(str(denominator)))
    if metric_type == "entity_count":
        return EntityCountExpr(
            entity=str(raw.get("entity") or ""),
            count_orders=(
                str(raw["count_orders"]) if raw.get("count_orders") else None
            ),
            min_orders=int(raw.get("min_orders", 2)),
        )
    return parse_expression(str(raw.get("expr") or ""))
