"""Deterministic recovery for failed plans.

SQL is never generated or repaired by a model: recovery only *removes*
constraints (a dimension, a comparison, a HAVING clause, a narrow time
window) and every candidate goes through the normal validator/compiler.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.plan import QueryPlan, TimeRange
from app.domain.semantic.loader import SemanticLayer


@dataclass
class RepairCandidate:
    plan: QueryPlan
    action: str
    detail: dict[str, Any]


def classify_error(exc: BaseException) -> tuple[str, str]:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "interrupt" in text or "out of memory" in text:
        return "TIMEOUT", "查询超时或资源不足"
    if "does not have a column named" in text:
        return "SCHEMA_MISMATCH", "指标依赖的列在数据库里不存在（语义层与数据不一致）"
    if "binder" in text or "catalog" in text or "not found" in text or "referenced column" in text:
        return "UNSUPPORTED_PLAN", "查询引擎无法解析这个维度组合"
    if "conversion" in text or "type" in text:
        return "TYPE_MISMATCH", "筛选值与列类型不匹配"
    return "EXECUTION_FAILED", "查询执行失败"


def candidates_for_failure(plan: QueryPlan, sem: SemanticLayer,
                           attempts_done: int = 0) -> list[RepairCandidate]:
    """Least-destructive first: the answer keeps as much of the question as
    the engine can actually execute."""
    candidates: list[RepairCandidate] = []

    def add(clone: QueryPlan, action: str, detail: dict) -> None:
        clone.degraded_reason = detail.get("reason")
        candidates.append(RepairCandidate(clone, action, detail))

    if plan.having:
        clone = plan.model_copy(deep=True)
        clone.having = []
        add(clone, "drop_having", {
            "reason": "分组后筛选条件无法执行，已取消",
            "removed": [spec.model_dump(mode="json") for spec in plan.having],
        })

    if plan.compare or plan.comparison:
        clone = plan.model_copy(deep=True)
        clone.compare = None
        clone.comparison = None
        add(clone, "drop_comparison", {
            "reason": "比较口径无法执行，已改为单期结果",
        })

    for dim_id in reversed(plan.dimensions):
        clone = plan.model_copy(deep=True)
        clone.dimensions = [d for d in clone.dimensions if d != dim_id]
        add(clone, "drop_dimension", {
            "reason": f"维度「{dim_id}」无法执行，已从结果中移除",
            "removed": dim_id,
        })

    if len(plan.dimensions) > 1:
        clone = plan.model_copy(deep=True)
        clone.dimensions = []
        add(clone, "drop_all_dimensions", {
            "reason": "按维度拆分无法执行，已退化为整体口径",
        })

    if plan.time is not None:
        lo, hi = sem.time_range
        if plan.time.start != lo or plan.time.end != hi:
            clone = plan.model_copy(deep=True)
            clone.time = TimeRange(start=lo, end=hi)
            add(clone, "widen_time", {
                "reason": f"已把时间范围放宽到数据覆盖 {lo} ~ {hi}",
            })

    # Never retry the exact plan we already ran.
    unique: list[RepairCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.plan.model_dump_json()
        if key == plan.model_dump_json() or key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
