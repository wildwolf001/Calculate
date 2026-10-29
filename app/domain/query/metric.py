"""Derived metric specs: a validated aggregate over a physical column.

The parser may propose a metric that is not published (for example
AVG(review_score)).  This module turns that proposal into a metric-shaped raw
dict the planner/compiler can consume without mutating the semantic layer.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.semantic.loader import SemanticError, SemanticLayer

AGGREGATIONS = {"SUM", "AVG", "COUNT", "MIN", "MAX"}


def build_metric_spec(spec: dict[str, Any], sem: SemanticLayer,
                      label: str | None = None) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise SemanticError("metric_expr 必须是对象")
    aggregation = str(spec.get("aggregation") or "").upper()
    operand = str(spec.get("operand") or "").strip()
    if aggregation not in AGGREGATIONS | {"COUNT_DISTINCT"}:
        raise SemanticError(f"不支持的聚合: {aggregation}")
    table, _, column = operand.partition(".")
    if not table or not column:
        raise SemanticError("metric_expr.operand 必须是 table.column")
    if table not in sem.tables:
        raise SemanticError(f"未知表: {table}")
    available = (sem.tables.get(table) or {}).get("columns") or []
    if available and column not in available:
        raise SemanticError(f"未知列: {table}.{column}")
    sem.column(f"{table}.{column}")  # validates the binding

    if aggregation == "COUNT_DISTINCT":
        expr = f"COUNT(DISTINCT {table}.{column})"
    else:
        expr = f"{aggregation}({table}.{column})"

    fact = str(spec.get("fact") or table)
    if fact not in sem.tables:
        raise SemanticError(f"未知事实表: {fact}")
    time_via = spec.get("time_via") or (sem.tables.get(fact) or {}).get("time")
    scope = dict(spec.get("scope") or {})
    supported = list(spec.get("supported_dimensions") or [])
    if not supported:
        supported = ["none", "month", *sem.dimensions.keys()]

    digest = hashlib.sha256(
        json.dumps(
            {"expr": expr, "fact": fact, "scope": scope, "time_via": time_via},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:10]
    return {
        "id": f"derived_{digest}",
        "name_zh": label or str(spec.get("label") or "派生指标"),
        "type": "aggregate",
        "fact": fact,
        "expr": expr,
        "scope": scope,
        "time_via": time_via,
        "unit": spec.get("unit"),
        "supported_grain": supported,
        "provenance": "inferred",
    }
