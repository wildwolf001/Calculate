"""Slots -> QueryPlan. Deterministic; no model calls here.

The parser resolves words to ids; the planner only fills defaults, normalizes
ordering/limits, and refuses to invent a metric.
"""
from __future__ import annotations

from app.config.settings import MAX_ROW_LIMIT
from app.core.state import Slots
from app.domain.plan import OrderSpec, QueryPlan, TimeRange
from app.domain.query.ir import QueryIR
from app.domain.semantic.graph import allowed_dimensions
from app.domain.semantic.loader import Metric, SemanticLayer, SemanticError


def degrade_unsupported_dimensions(
    plan: QueryPlan, sem: SemanticLayer
) -> tuple[QueryPlan, list[str]] | None:
    """Drop dimensions the metric cannot group by, keeping an overall result.

    The caller records the degradation so the answer can say what was removed
    and still show the verifiable overall value.
    """
    if plan.kind != "aggregate" or not plan.metric:
        return None
    if plan.metric_spec:
        metric = Metric(plan.metric, plan.metric_spec)
    else:
        metric = sem.metric(plan.metric)
    allowed = allowed_dimensions(sem, metric.id) if not plan.metric_spec else set(
        metric.raw.get("supported_grain") or ["none"]
    )
    removed = [d for d in plan.dimensions if d not in allowed]
    if not removed:
        return None
    kept = [d for d in plan.dimensions if d in allowed]
    names = "、".join(
        sem.dimension(d).raw.get("name_zh", d) for d in removed
    )
    reason = f"指标「{metric.name}」不支持按「{names}」拆分，已退化为整体口径。"
    degraded = plan.model_copy(deep=True)
    degraded.dimensions = kept
    degraded.degraded_reason = reason
    degraded.degraded_kind = "dimension"
    degraded.notes = list(degraded.notes) + [reason]
    return degraded, removed


def member_list_plan(dim: str | None, slots: Slots, sem: SemanticLayer) -> QueryPlan:
    """Plan a dimension-member listing (the second plan shape).

    A list is a query with a scope, not a separate executor: filters and time
    travel through the same plan -> validate -> compile -> check pipeline.
    """
    if not dim:
        raise SemanticError("没有识别出要列举的维度")
    if not sem.capabilities.get("member_list", True):
        raise SemanticError("当前数据源未启用维度成员清单能力")
    d = sem.dimension(dim)
    raw = d.raw
    if raw.get("kind") in {"time", "derived"} or not raw.get("column"):
        raise SemanticError(f"维度「{raw.get('name_zh', dim)}」不支持枚举")

    notes = list(slots.notes)
    coverage = sem.capabilities.get("member_list_coverage") or {}
    effective_time = slots.time
    degraded_reason = None
    if coverage.get("label"):
        notes.append(f"成员清单来自维度表；{coverage['label']}为该成员覆盖的统计值。")
    else:
        notes.append("成员清单来自维度表。")
    if slots.time:
        base_table = str(raw.get("column", "")).split(".")[0]
        if coverage.get("time_column") or (sem.tables.get(base_table) or {}).get("time"):
            notes.append(
                f"时间 {slots.time.start}~{slots.time.end} 同时约束成员的时间覆盖。"
            )
        else:
            effective_time = None
            degraded_reason = (
                "当前维度的成员清单没有时间覆盖列，已忽略时间范围并返回完整成员集合。"
            )
            notes.append(degraded_reason)
    # A member list is exhaustive by nature; truncation is reported, not
    # silently capped at the aggregate default of 100.
    limit = MAX_ROW_LIMIT
    return QueryPlan(
        kind="member_list",
        plan_id=f"list_{dim}",
        list_dim=dim,
        filters=slots.filters,
        time=effective_time,
        order_by=slots.order_by,
        limit=limit,
        notes=notes,
        degraded_reason=degraded_reason,
    )


def coverage_plan(slots: Slots, sem: SemanticLayer) -> QueryPlan:
    """Build a period-coverage diagnostic over an aggregate metric."""
    metric_id = slots.metric
    if metric_id is None:
        candidates = [
            metric for metric in sem.metrics.values()
            if metric.raw.get("selected", True)
            and metric.type == "aggregate"
            and metric.raw.get("time_via")
        ]
        candidates.sort(
            key=lambda metric: (
                str(metric.raw.get("analysis_role") or "") not in {
                    "primary_sales", "sales_primary",
                },
                "销售额" not in metric.name and "销售" not in metric.name,
                "记录数" in metric.name,
                metric.name,
            )
        )
        if not candidates:
            raise SemanticError("覆盖诊断需要至少一个带时间列的聚合指标")
        metric_id = candidates[0].id
    metric = sem.metric(metric_id)
    if metric.type != "aggregate":
        raise SemanticError("覆盖诊断目前只支持聚合指标")
    if not metric.raw.get("time_via"):
        raise SemanticError(f"指标「{metric.name}」没有时间列，无法诊断时间段覆盖")
    time = slots.time
    if time is None:
        lo, hi = sem.time_range
        time = TimeRange(start=lo, end=hi)
    return QueryPlan(
        kind="coverage",
        plan_id=f"coverage_{metric.id}",
        metric=metric.id,
        dimensions=[],
        time=time,
        filters=slots.filters,
        limit=MAX_ROW_LIMIT,
        notes=list(slots.notes) + [
            "覆盖诊断使用完整自然月序列，并区分无记录月份与零业务量月份。"
        ],
        coverage_period="month",
    )


def plan_from_ir(ir: QueryIR, sem: SemanticLayer) -> QueryPlan:
    """IR is the planning contract; Slots is only its legacy container."""
    return plan_from_slots(ir.to_slots(), sem)


def plan_from_slots(slots: Slots, sem: SemanticLayer) -> QueryPlan:
    if slots.intent == "coverage":
        return coverage_plan(slots, sem)
    derived_spec: dict | None = None
    if slots.metric_spec:
        spec = slots.metric_spec
        derived_spec = spec.to_raw()
        metric = Metric(spec.id, derived_spec)
    else:
        metric_id = slots.ratio_numerator or slots.metric
        if not metric_id:
            raise SemanticError("没有识别出可查询的指标")
        metric = sem.metric(metric_id)

    time = slots.time
    notes = list(slots.notes)
    if time is None:
        if slots.comparison:
            time = slots.comparison.current
        elif metric.raw.get("time_via"):
            lo, hi = sem.time_range
            time = TimeRange(start=lo, end=hi)
            notes.append(
                f"未指定时间，默认使用数据全集 {lo} ~ {hi}（右开区间，与全量口径等价）。"
            )
        else:
            notes.append("该指标没有时间列，按全部数据统计。")

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
        if metric.type in {"ratio"} or slots.ratio_denominator:
            order_by = [OrderSpec(field="value", direction="desc")]
        else:
            order_by = []

    limit = max(1, min(int(slots.limit), MAX_ROW_LIMIT))
    limit_per_group = slots.limit_per_group
    if limit_per_group and len(dims) < 2:
        # "哪个商家最高" is a global top-N, not a per-group top-N. Keeping
        # limit_per_group with one dimension would force a needless LLM repair.
        limit = max(1, min(int(limit_per_group), MAX_ROW_LIMIT))
        limit_per_group = None
        notes.append("只有一个分组维度，按整体排序取前 N。")

    plan = QueryPlan(
        plan_id=(
            f"p_{metric.id}_over_{slots.ratio_denominator}"
            if slots.ratio_denominator else f"p_{metric.id}"
        ),
        metric=metric.id,
        metric_spec=derived_spec,
        metric_source=slots.metric_source or ("inferred" if derived_spec else None),
        ratio_numerator=slots.ratio_numerator,
        ratio_denominator=slots.ratio_denominator,
        dimensions=dims,
        time=time,
        time_axis=slots.time_axis,
        filters=slots.filters,
        having=slots.having,
        ratio_grain=metric.raw.get("ratio_grain") if metric.type == "ratio" else None,
        nested=slots.nested,
        order_by=order_by,
        limit=limit,
        limit_per_group=limit_per_group,
        share=bool(slots.share),
        compare=slots.compare,
        comparison=slots.comparison,
        notes=notes,
        visualization=slots.visualization,
    )
    return plan
