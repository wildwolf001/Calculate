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


def _normalize_time_via(ref: Any, sem: SemanticLayer, fact: str) -> str | None:
    text = str(ref or "").strip() or str((sem.tables.get(fact) or {}).get("time") or "").strip()
    if text and "." not in text:
        text = f"{fact}.{text}"
    return text or None


def _supported_dimensions(sem: SemanticLayer, fact: str) -> list[str]:
    """Only N:1-reachable dimensions; a derived aggregate cannot fan out."""
    from app.domain.semantic.graph import _n1_tables

    reachable = _n1_tables(sem, fact)
    out = ["none", "month"]
    for dim_id, dim in sem.dimensions.items():
        if dim.kind == "time":
            continue
        if dim.kind == "derived":
            table = str(dim.raw.get("requires") or "")
        else:
            table = str(dim.raw.get("column") or "").split(".", 1)[0]
        if table and table in reachable and dim_id not in out:
            out.append(dim_id)
    return out


def _digest(expr: str, fact: str, scope: dict[str, Any],
            time_via: str | None) -> str:
    return hashlib.sha256(
        json.dumps(
            {"expr": expr, "fact": fact, "scope": scope, "time_via": time_via},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:10]


def build_metric_spec(spec: dict[str, Any], sem: SemanticLayer,
                      label: str | None = None) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise SemanticError("metric_expr 必须是对象")
    # Idempotent: the parser stores the normalized raw spec in Slots, then the
    # planner may pass it back here. Accept both the input shape (aggregation +
    # operand) and the normalized shape (expr + fact) instead of rejecting the
    # second pass with an empty aggregation.
    if spec.get("expr") and not spec.get("aggregation"):
        fact = str(spec.get("fact") or "").strip()
        if not fact or fact not in sem.tables:
            raise SemanticError(f"未知事实表: {fact}")
        expr = str(spec.get("expr") or "").strip()
        scope = dict(spec.get("scope") or {})
        time_via = _normalize_time_via(spec.get("time_via"), sem, fact)
        return {
            "id": str(spec.get("id") or f"derived_{_digest(expr, fact, scope, time_via)}"),
            "name_zh": label or str(spec.get("name_zh") or spec.get("label") or "派生指标"),
            "type": "aggregate",
            "fact": fact,
            "expr": expr,
            "scope": scope,
            "time_via": time_via,
            "unit": spec.get("unit"),
            "supported_grain": _supported_dimensions(sem, fact),
            "provenance": spec.get("provenance") or "inferred",
        }

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
    time_via = _normalize_time_via(spec.get("time_via"), sem, fact)
    scope = dict(spec.get("scope") or {})
    supported = _supported_dimensions(sem, fact)

    digest = _digest(expr, fact, scope, time_via)
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
