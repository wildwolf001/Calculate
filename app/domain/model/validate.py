"""Semantic checks that do not depend on a specific dataset.

These are model-level invariants: grain, ratio composition, relation
cardinality and declared time axes. They are advisory until the compiler and
publish path consume them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.model.expr import (
    AggExpr,
    CountDistinctExpr,
    FilteredExpr,
    RatioExpr,
    RawExpr,
)
from app.domain.model.model import SemanticModel

COLUMN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z0-9_]+\b")
LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


@dataclass(frozen=True)
class SemanticIssue:
    code: str
    object_id: str
    message: str
    severity: str = "warn"

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "object_id": self.object_id,
            "message": self.message,
            "severity": self.severity,
        }


def validate_semantics(model: SemanticModel) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    for metric in model.metrics.values():
        expr = metric.expr
        if isinstance(expr, AggExpr):
            if expr.aggregation == "AVG" and not metric.grain:
                issues.append(SemanticIssue(
                    "AVG_GRAIN_MISSING", metric.id,
                    f"AVG 指标「{metric.name}」没有声明 grain，无法判断按实体还是按记录平均",
                ))
        if isinstance(expr, RatioExpr):
            if not metric.grain:
                issues.append(SemanticIssue(
                    "RATIO_GRAIN_MISSING", metric.id,
                    f"比率「{metric.name}」没有声明 grain，分子分母可能无法对齐",
                    severity="error",
                ))
            if isinstance(expr.numerator, CountDistinctExpr) and isinstance(
                expr.denominator, CountDistinctExpr
            ):
                issues.append(SemanticIssue(
                    "RATIO_DOUBLE_DISTINCT", metric.id,
                    f"比率「{metric.name}」的分子分母都是 DISTINCT 计数，请确认粒度语义",
                    severity="info",
                ))
            numerator_literals = _expr_literals(expr.numerator)
            denominator_literals = _expr_literals(expr.denominator)
            if numerator_literals != denominator_literals:
                issues.append(SemanticIssue(
                    "RATIO_SCOPE_MISMATCH", metric.id,
                    f"比率「{metric.name}」的分子/分母字面过滤不同 "
                    f"({sorted(numerator_literals)} vs {sorted(denominator_literals)})，"
                    "请确认 scope 是否一致",
                    severity="info",
                ))
            numerator_tables = _expr_tables(expr.numerator)
            denominator_tables = _expr_tables(expr.denominator)
            if numerator_tables and denominator_tables and not (
                numerator_tables & denominator_tables
            ):
                issues.append(SemanticIssue(
                    "RATIO_TABLE_MISMATCH", metric.id,
                    f"比率「{metric.name}」的分子与分母引用不同表 "
                    f"({sorted(numerator_tables)} vs {sorted(denominator_tables)})，"
                    "需要确认 grain/join 是否对齐",
                ))
        if metric.time_axis and metric.time_axis not in model.time_axes:
            issues.append(SemanticIssue(
                "TIME_AXIS_UNDECLARED", metric.id,
                f"指标「{metric.name}」引用了未声明的时间轴 {metric.time_axis}",
            ))
    for relation in model.relations:
        if relation.cardinality not in {"N:1", "1:1"}:
            issues.append(SemanticIssue(
                "RELATION_CARDINALITY", f"{relation.child}->{relation.parent}",
                "关系不是 N:1/1:1，join 可能放大行数",
            ))
    return issues

def _expr_literals(expr) -> set[str]:
    texts: list[str] = []
    if isinstance(expr, (AggExpr, CountDistinctExpr)):
        texts.append(expr.operand)
    elif isinstance(expr, RatioExpr):
        return _expr_literals(expr.numerator) | _expr_literals(expr.denominator)
    elif isinstance(expr, FilteredExpr):
        return _expr_literals(expr.metric) | {
            value
            for pair in LITERAL_RE.findall(expr.predicate)
            for value in pair
            if value
        }
    elif isinstance(expr, RawExpr):
        texts.append(expr.expr)
    values: set[str] = set()
    for text in texts:
        for pair in LITERAL_RE.findall(text):
            values.update(value for value in pair if value)
    return values


def _expr_tables(expr) -> set[str]:
    if isinstance(expr, (AggExpr, CountDistinctExpr)):
        return set(COLUMN_RE.findall(expr.operand))
    if isinstance(expr, RatioExpr):
        return _expr_tables(expr.numerator) | _expr_tables(expr.denominator)
    if isinstance(expr, FilteredExpr):
        return _expr_tables(expr.metric) | set(COLUMN_RE.findall(expr.predicate))
    if isinstance(expr, RawExpr):
        return set(COLUMN_RE.findall(expr.expr))
    return set()

