"""Two-stage validation: plan contract, then SQL safety.

The plan stage catches unsupported business combinations *before* SQL exists.
The SQL stage is a defence-in-depth net (single read-only SELECT, table
whitelist, required LIMIT, time filter when the metric has one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config.settings import MAX_ROW_LIMIT
from app.domain.compiler import Compiled
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import Metric, SemanticLayer

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|"
    r"COPY|EXPORT|IMPORT|PRAGMA|CALL|INSTALL|LOAD|VACUUM|CHECKPOINT)\b",
    re.IGNORECASE,
)


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "error"      # error | warn

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "severity": self.severity}


def validate_plan(plan: QueryPlan, sem: SemanticLayer) -> list[Issue]:
    issues: list[Issue] = []
    if plan.kind == "member_list":
        return _validate_member_list(plan, sem)
    if plan.kind == "coverage":
        return _validate_coverage(plan, sem)

    try:
        if plan.metric_spec:
            metric = Metric(plan.metric, plan.metric_spec)
        else:
            metric = sem.metric(plan.metric)
    except Exception as exc:
        return [Issue("UNKNOWN_METRIC", str(exc))]

    if plan.ratio_denominator:
        try:
            denominator = sem.metric(plan.ratio_denominator)
        except Exception as exc:
            issues.append(Issue("UNKNOWN_METRIC", str(exc)))
        else:
            if metric.fact != denominator.fact:
                from app.domain.semantic.graph import common_grain_table

                common = common_grain_table(sem, metric.fact, denominator.fact)
                cross_fact_ok = bool(common)
                if not cross_fact_ok:
                    issues.append(Issue(
                        "METRIC_RATIO_FACT_MISMATCH",
                        "指标比率的分子和分母来自不同事实表，且没有可用的共同 grain；"
                        "请先对齐 grain/join，或不要分组/筛选",
                    ))
            if (metric.raw.get("scope") or {}) != (denominator.raw.get("scope") or {}):
                issues.append(Issue(
                    "METRIC_RATIO_SCOPE_MISMATCH",
                    "指标比率的分子和分母 scope 不一致",
                ))
            if (metric.raw.get("time_via") or "") != (denominator.raw.get("time_via") or ""):
                issues.append(Issue(
                    "METRIC_RATIO_TIME_MISMATCH",
                    "指标比率的分子和分母时间轴不一致",
                ))

    if len(set(plan.dimensions)) != len(plan.dimensions):
        issues.append(Issue("DUPLICATE_DIMENSION", "维度重复"))

    from app.domain.semantic.graph import build_capabilities

    capability = None if plan.metric_spec else build_capabilities(sem).get(metric.id)
    allowed_grains = (
        set(capability.allowed_dimensions)
        if capability else set(metric.raw.get("supported_grain") or ["none"])
    )

    for d in plan.dimensions:
        if d == "none":
            continue
        if d not in sem.dimensions:
            issues.append(Issue("UNKNOWN_DIMENSION", f"未知维度: {d}"))
        elif d not in allowed_grains:
            fanout_note = (
                "；该维度需要经过一对多连接，会放大行数"
                if capability and d in capability.fanout_dimensions else ""
            )
            issues.append(Issue(
                "UNSUPPORTED_GRAIN",
                f"指标「{metric.name}」不支持按「{sem.dimensions[d].raw.get('name_zh', d)}」拆分"
                f"{fanout_note}"
                f"（支持: {', '.join(sorted(allowed_grains - {'none'}))}）",
            ))

    allowed_filters = metric.raw.get("filter_grain")
    for key in plan.filters:
        if key not in sem.dimensions:
            issues.append(Issue("UNKNOWN_FILTER", f"未知过滤字段: {key}"))
        elif allowed_filters is not None and key not in allowed_filters:
            name = sem.dimensions[key].raw.get("name_zh", key)
            issues.append(Issue(
                "UNSUPPORTED_FILTER",
                f"指标「{metric.name}」不支持按「{name}」筛选："
                "该指标只能按订单级维度过滤，明细级条件会被摊到整单。",
            ))

    if plan.time_axis:
        from app.domain.semantic.graph import time_axis_allowed

        if not time_axis_allowed(sem, metric, plan.time_axis):
            issues.append(Issue(
                "UNSUPPORTED_TIME_AXIS",
                f"时间轴 {plan.time_axis} 不可用：必须是该指标可达的时间列",
            ))

    if plan.time:
        if not (metric.raw.get("time_via") or plan.time_axis):
            issues.append(Issue(
                "TIME_UNSUPPORTED",
                f"指标「{metric.name}」没有时间列，不能按时间筛选",
            ))
        if plan.time.start >= plan.time.end:
            issues.append(Issue("BAD_TIME_RANGE", "时间范围非法"))
        lo, hi = sem.time_range
        if str(plan.time.start) < lo or str(plan.time.end) > hi:
            issues.append(Issue(
                "TIME_OUT_OF_DATA",
                f"时间范围 {plan.time.start}~{plan.time.end} 超出数据覆盖 {lo}~{hi}，结果可能为空",
                "warn",
            ))

    if plan.limit < 1 or plan.limit > MAX_ROW_LIMIT:
        issues.append(Issue("BAD_LIMIT", f"limit 必须在 1..{MAX_ROW_LIMIT}"))

    if plan.share:
        if not plan.dimensions:
            issues.append(Issue("BAD_SHARE", "占比需要至少一个分组维度"))
        if not plan.ratio_denominator and (
            metric.type != "aggregate" or not metric.raw.get("additive")
        ):
            issues.append(Issue("BAD_SHARE", "占比只支持可加聚合指标"))
        if plan.compare or plan.comparison:
            issues.append(Issue("BAD_SHARE", "占比不能与期间比较同时使用"))
        if plan.limit_per_group:
            issues.append(Issue("BAD_SHARE", "占比不能与每组 Top-N 同时使用"))

    if plan.limit_per_group is not None:
        if plan.kind != "aggregate":
            issues.append(Issue("BAD_PER_GROUP", "每组 Top-N 只用于聚合查询"))
        elif len(plan.dimensions) < 2:
            issues.append(Issue(
                "BAD_PER_GROUP", "每组 Top-N 需要至少两个维度（分组维度 + 排名维度）",
            ))
        if not 1 <= int(plan.limit_per_group) <= 50:
            issues.append(Issue("BAD_PER_GROUP", "每组条数必须在 1..50"))

    if plan.compare and metric.type not in {"aggregate", "ratio"}:
        issues.append(Issue("COMPARE_UNSUPPORTED",
                            "环比/同比仅支持可加聚合指标和比率指标"))
    if plan.comparison and metric.type not in {"aggregate", "ratio"}:
        issues.append(Issue("COMPARE_UNSUPPORTED",
                            "显式期间比较仅支持可加聚合指标和比率指标"))

    if plan.comparison:
        cur, base = plan.comparison.current, plan.comparison.baseline
        lo, hi = sem.time_range
        for name, rng in (("current", cur), ("baseline", base)):
            if rng.start >= rng.end:
                issues.append(Issue("BAD_COMPARISON", f"{name} 期间为空"))
            if str(rng.start) < lo or str(rng.end) > hi:
                issues.append(Issue(
                    "TIME_OUT_OF_DATA",
                    f"{name} 期间 {rng.start}~{rng.end} 超出数据覆盖 {lo}~{hi}",
                    "warn",
                ))
        if cur.start == base.start and cur.end == base.end:
            issues.append(Issue("SAME_COMPARISON_PERIOD", "比较的两个期间完全相同"))

    if plan.nested and metric.type != "entity_count":
        issues.append(Issue("NESTED_UNSUPPORTED", "nested 参数只用于实体计数指标"))

    if plan.ratio_grain and metric.type != "ratio":
        issues.append(Issue("RATIO_GRAIN_UNUSED", "ratio_grain 只用于比率指标", "warn"))

    if metric.type == "ratio" and not plan.ratio_grain:
        plan.ratio_grain = metric.raw.get("ratio_grain") or "order"

    for h in plan.having:
        if h.metric not in {"order_count", "value", plan.metric}:
            issues.append(Issue("BAD_HAVING", f"HAVING 不支持字段 {h.metric}"))
    return issues


def _validate_coverage(plan: QueryPlan, sem: SemanticLayer) -> list[Issue]:
    issues: list[Issue] = []
    try:
        metric = sem.metric(plan.metric)
    except Exception as exc:
        return [Issue("UNKNOWN_METRIC", str(exc))]
    if metric.type != "aggregate":
        issues.append(Issue("COVERAGE_METRIC_UNSUPPORTED",
                            "覆盖诊断目前只支持聚合指标"))
    if not metric.raw.get("time_via"):
        issues.append(Issue("COVERAGE_TIME_UNSUPPORTED",
                            f"指标「{metric.name}」没有时间列"))
    if plan.time is None:
        issues.append(Issue("COVERAGE_TIME_REQUIRED", "覆盖诊断需要明确时间范围"))
    elif plan.time.start >= plan.time.end:
        issues.append(Issue("BAD_TIME_RANGE", "时间范围非法"))
    if plan.dimensions:
        issues.append(Issue("COVERAGE_DIMENSIONS_UNUSED",
                            "覆盖诊断由月份序列驱动，不需要额外维度", "warn"))
    for key in plan.filters:
        if key not in sem.dimensions:
            issues.append(Issue("UNKNOWN_FILTER", f"未知过滤字段: {key}"))
    if plan.limit < 1 or plan.limit > MAX_ROW_LIMIT:
        issues.append(Issue("BAD_LIMIT", f"limit 必须在 1..{MAX_ROW_LIMIT}"))
    return issues


def _validate_member_list(plan: QueryPlan, sem: SemanticLayer) -> list[Issue]:
    issues: list[Issue] = []
    dim_id = plan.list_dim
    if not dim_id or dim_id not in sem.dimensions:
        issues.append(Issue("UNKNOWN_DIMENSION", f"未知枚举维度: {dim_id}"))
    else:
        raw = sem.dimensions[dim_id].raw
        if raw.get("kind") in {"time", "derived"} or not raw.get("column"):
            issues.append(Issue(
                "UNSUPPORTED_LIST_DIMENSION",
                f"维度「{raw.get('name_zh', dim_id)}」不支持枚举（需要物理列）",
            ))

    for key in plan.filters:
        dim = sem.dimensions.get(key)
        if dim is None:
            issues.append(Issue("UNKNOWN_FILTER", f"未知过滤字段: {key}"))
        elif not (dim.raw.get("filter_column") or dim.raw.get("column")):
            issues.append(Issue("UNSUPPORTED_FILTER", f"维度「{key}」不能作为过滤条件"))
    if isinstance(plan.metric, str):
        issues.append(Issue("METRIC_ON_LIST", "成员枚举计划不应携带指标", "warn"))
    if plan.dimensions:
        issues.append(Issue("DIMENSIONS_ON_LIST", "成员枚举计划不应携带输出维度", "warn"))

    if plan.time:
        if plan.time.start >= plan.time.end:
            issues.append(Issue("BAD_TIME_RANGE", "时间范围非法"))
        lo, hi = sem.time_range
        if str(plan.time.start) < lo or str(plan.time.end) > hi:
            issues.append(Issue(
                "TIME_OUT_OF_DATA",
                f"时间范围 {plan.time.start}~{plan.time.end} 超出数据覆盖 {lo}~{hi}，结果可能为空",
                "warn",
            ))

    if plan.limit < 1 or plan.limit > MAX_ROW_LIMIT:
        issues.append(Issue("BAD_LIMIT", f"limit 必须在 1..{MAX_ROW_LIMIT}"))
    return issues


def validate_sql(sql: str, compiled: Compiled, plan: QueryPlan, sem: SemanticLayer) -> list[Issue]:
    issues: list[Issue] = []
    body = sql.strip()

    if body.count(";") > 0:
        issues.append(Issue("MULTI_STATEMENT", "SQL 只能包含单条语句"))
    head = body.lstrip("(").split(None, 1)[0].upper() if body else ""
    if head not in {"SELECT", "WITH"}:
        issues.append(Issue("NOT_SELECT", "只允许 SELECT/WITH 查询"))
    if FORBIDDEN.search(body):
        issues.append(Issue("FORBIDDEN_KEYWORD", "SQL 含禁止关键字"))
    if not re.search(r"\bLIMIT\s+\d+", body, re.IGNORECASE):
        issues.append(Issue("MISSING_LIMIT", "SQL 必须有外层 LIMIT"))

    ctes = set(re.findall(
        r"\bWITH\s+([A-Za-z_][A-Za-z0-9_]*)|,\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(",
        body,
    ))
    cte_names = {a or b for a, b in ctes}
    referenced = set(re.findall(r"\b(?:FROM|JOIN)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", body, re.IGNORECASE))
    allowed = set(sem.tables) | cte_names
    unknown = referenced - allowed
    if unknown:
        issues.append(Issue("UNKNOWN_TABLE", f"SQL 引用了白名单之外的表: {sorted(unknown)}"))

    if plan.time and not compiled.applied_time_filter:
        issues.append(Issue("MISSING_TIME_FILTER", "计划有明确时间范围但 SQL 未注入时间过滤"))

    return issues
