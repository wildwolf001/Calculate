"""Slots -> QueryPlan. Deterministic; no model calls here.

The parser resolves words to ids; the planner only fills defaults, normalizes
ordering/limits, and refuses to invent a metric.
"""
from __future__ import annotations

from app.config.settings import MAX_ROW_LIMIT
from app.core.state import Slots
from app.domain.plan import OrderSpec, QueryPlan, TimeRange
from app.domain.semantic.loader import SemanticLayer, SemanticError


def degrade_unsupported_dimensions(
    plan: QueryPlan, sem: SemanticLayer
) -> tuple[QueryPlan, list[str]] | None:
    """Drop dimensions the metric cannot group by, keeping an overall result.

    The caller records the degradation so the answer can say what was removed
    and still show the verifiable overall value.
    """
    if plan.kind != "aggregate" or not plan.metric:
        return None
    metric = sem.metric(plan.metric)
    removed = [d for d in plan.dimensions if d not in metric.supported_grain]
    if not removed:
        return None
    kept = [d for d in plan.dimensions if d in metric.supported_grain]
    names = "、".join(
        sem.dimension(d).raw.get("name_zh", d) for d in removed
    )
    reason = f"指标「{metric.name}」不支持按「{names}」拆分，已退化为整体口径。"
    degraded = plan.model_copy(deep=True)
    degraded.dimensions = kept
    degraded.degraded_reason = reason
    degraded.notes = list(degraded.notes) + [reason]
    return degraded, removed


def member_list_plan(dim: str | None, slots: Slots, sem: SemanticLayer) -> QueryPlan:
    """Plan a dimension-member listing (the second plan shape).

    A list is a query with a scope, not a separate executor: filters and time
    travel through the same plan -> validate -> compile -> check pipeline.
    """
    if not dim:
        raise SemanticError("没有识别出要列举的维度")
    d = sem.dimension(dim)
    raw = d.raw
    if raw.get("kind") in {"time", "derived"} or not raw.get("column"):
        raise SemanticError(f"维度「{raw.get('name_zh', dim)}」不支持枚举")

    notes = list(slots.notes)
    notes.append("成员清单来自维度表；order_count 为该成员覆盖的已送达订单数。")
    if slots.time:
        notes.append(
            f"时间 {slots.time.start}~{slots.time.end} 同时约束成员：只保留该期间"
            "有已送达订单的成员，覆盖数也只统计期间内订单。"
        )
    # A member list is exhaustive by nature; truncation is reported, not
    # silently capped at the aggregate default of 100.
    limit = MAX_ROW_LIMIT
    return QueryPlan(
        kind="member_list",
        plan_id=f"list_{dim}",
        list_dim=dim,
        filters=slots.filters,
        time=slots.time,
        order_by=slots.order_by,
        limit=limit,
        notes=notes,
    )


def plan_from_slots(slots: Slots, sem: SemanticLayer) -> QueryPlan:
    if not slots.metric:
        raise SemanticError("没有识别出可查询的指标")
    metric = sem.metric(slots.metric)

    time = slots.time
    notes = list(slots.notes)
    if time is None:
        if slots.comparison:
            time = slots.comparison.current
        else:
            lo, hi = sem.time_range
            time = TimeRange(start=lo, end=hi)
            notes.append(
                f"未指定时间，默认使用数据全集 {lo} ~ {hi}（右开区间，与全量口径等价）。"
            )

    dims = [d for d in slots.dimensions if d != "none"]
    if slots.comparison and dims == ["month"]:
        # The comparison itself defines the two periods; a month dimension
        # would produce one row per period instead of one comparison row.
        dims = []
        notes.append("比较问题按两个显式期间对照，不再按月份展开。")
    for d in dims:
        sem.dimension(d)

    order_by = list(slots.order_by)
    if not order_by and dims:
        # deterministic default: sort by the value for ranking-style metrics,
        # otherwise keep dimension order (stable, explainable)
        order_by = [OrderSpec(field="value", direction="desc")] if metric.type in {"ratio"} else []

    limit = max(1, min(int(slots.limit), MAX_ROW_LIMIT))

    plan = QueryPlan(
        plan_id=f"p_{metric.id}",
        metric=metric.id,
        dimensions=dims,
        time=time,
        filters=slots.filters,
        having=slots.having,
        ratio_grain=metric.raw.get("ratio_grain") if metric.type == "ratio" else None,
        nested=slots.nested,
        order_by=order_by,
        limit=limit,
        compare=slots.compare,
        comparison=slots.comparison,
        notes=notes,
        visualization=slots.visualization,
    )
    return plan
