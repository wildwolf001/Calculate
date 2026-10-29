"""MetricExpr to SQL primitives.

This is the model-side compiler contract; the legacy compiler still consumes
raw metric strings until the migration is complete.
"""
from __future__ import annotations

from typing import Any

from app.domain.model.expr import (
    AggExpr,
    CountDistinctExpr,
    MetricExpr,
    RatioExpr,
    RawExpr,
)


def compile_metric_expr(expr: MetricExpr, sem: Any, fact: str | None = None) -> str:
    if isinstance(expr, AggExpr):
        operand = _operand(expr.operand, sem)
        return f"{expr.aggregation}({operand})"
    if isinstance(expr, CountDistinctExpr):
        return f"COUNT(DISTINCT {_operand(expr.operand, sem)})"
    if isinstance(expr, RatioExpr):
        numerator = compile_metric_expr(expr.numerator, sem, fact)
        denominator = compile_metric_expr(expr.denominator, sem, fact)
        return f"({numerator}) / NULLIF(({denominator}), 0)"
    if isinstance(expr, RawExpr):
        return expr.expr
    raise TypeError(f"unsupported metric expression: {type(expr).__name__}")


def _operand(operand: str, sem: Any) -> str:
    text = str(operand or "").strip()
    if text == "*":
        return "*"
    if text.count(".") == 1 and " " not in text:
        table, _, column = text.partition(".")
        if table and column:
            return f'"{table}"."{sem.column(text)}"'
    return text
