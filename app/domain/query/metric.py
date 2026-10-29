"""Typed derived metric specification.

This is the single shape that crosses the parser/planner boundary.  It is a
Pydantic model, not a mutable dict: the parser creates it once, the planner
consumes it directly, and only the compiler boundary converts it to the raw
metric dict it already understands.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    fact: str
    expr: str
    scope: dict[str, Any] = Field(default_factory=dict)
    time_via: str | None = None
    unit: str | None = None
    supported_grain: list[str] = Field(default_factory=list)
    provenance: str = "inferred"

    @classmethod
    def from_input(cls, spec: dict[str, Any], sem: SemanticLayer,
                   label: str | None = None) -> "MetricSpec":
        """LLM shape: {aggregation, operand, fact, scope, time_via, label}."""
        if not isinstance(spec, dict):
            raise SemanticError("metric_expr 必须是对象")
        if spec.get("expr") and not spec.get("aggregation"):
            return cls.from_raw(spec)
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
        sem.column(f"{table}.{column}")
        expr = (
            f"COUNT(DISTINCT {table}.{column})"
            if aggregation == "COUNT_DISTINCT"
            else f"{aggregation}({table}.{column})"
        )
        fact = str(spec.get("fact") or table)
        if fact not in sem.tables:
            raise SemanticError(f"未知事实表: {fact}")
        scope = dict(spec.get("scope") or {})
        time_via = _normalize_time_via(spec.get("time_via"), sem, fact)
        return cls(
            id=f"derived_{_digest(expr, fact, scope, time_via)}",
            name=str(label or spec.get("label") or "派生指标"),
            fact=fact,
            expr=expr,
            scope=scope,
            time_via=time_via,
            unit=spec.get("unit"),
            supported_grain=_supported_dimensions(sem, fact),
            provenance="inferred",
        )

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "MetricSpec":
        """Compiler/metric shape: {id, name_zh, fact, expr, scope, ...}."""
        fact = str(raw.get("fact") or "").strip()
        expr = str(raw.get("expr") or "").strip()
        if not fact or not expr:
            raise SemanticError("metric_spec 缺少 fact/expr")
        return cls(
            id=str(raw.get("id") or f"derived_{_digest(expr, fact, {}, None)}"),
            name=str(raw.get("name_zh") or raw.get("name") or "派生指标"),
            fact=fact,
            expr=expr,
            scope=dict(raw.get("scope") or {}),
            time_via=raw.get("time_via"),
            unit=raw.get("unit"),
            supported_grain=list(raw.get("supported_grain") or []),
            provenance=str(raw.get("provenance") or "inferred"),
        )

    def to_raw(self) -> dict[str, Any]:
        return {
            "name_zh": self.name,
            "type": "aggregate",
            "fact": self.fact,
            "expr": self.expr,
            "scope": dict(self.scope),
            "time_via": self.time_via,
            "unit": self.unit,
            "supported_grain": list(self.supported_grain),
            "provenance": self.provenance,
        }
