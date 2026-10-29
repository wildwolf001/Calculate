"""Deterministic capability assessment.

Before a plan is built, answer one question: *can this dataset answer the
request at all?*  The result drives two things:

1. deterministic degradation (drop a dimension the metric cannot split by),
2. a structured refusal (what is missing, what is available, what else can be
   asked) instead of letting the model invent an explanation.

No LLM, no SQL, no database access: everything here is derived from the
semantic layer's own declarations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.domain.plan import TimeRange
from app.domain.semantic.loader import Metric
from app.domain.semantic.graph import (
    allowed_dimensions,
    build_capabilities,
    time_axis_allowed,
)


@dataclass
class Missing:
    kind: str                 # metric | dimension | filter | time | data | policy
    name: str
    reason: str
    id: str | None = None
    code: str | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "name": self.name,
                "reason": self.reason, "code": self.code}


@dataclass
class CapabilityReport:
    status: str                                   # answerable | degradable | refusable
    metric: str | None = None
    missing: list[Missing] = field(default_factory=list)
    degradations: list[dict] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    data_health: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.status == "refusable"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "metric": self.metric,
            "missing": [item.as_dict() for item in self.missing],
            "degradations": list(self.degradations),
            "alternatives": list(self.alternatives),
            "data_health": list(self.data_health),
            "notes": list(self.notes),
        }

    def refusal_payload(self) -> dict:
        code = "UNABLE_TO_ANSWER"
        if self.missing:
            code = self.missing[0].code or {
                "metric": "MISSING_METRIC",
                "dimension": "MISSING_DIMENSION",
                "filter": "MISSING_FILTER",
                "time": "NO_TIME_DATA",
                "data": "DATA_UNAVAILABLE",
                "policy": "ACCESS_DENIED",
            }.get(self.missing[0].kind, "UNABLE_TO_ANSWER")
        return {
            "code": code,
            "missing": [item.as_dict() for item in self.missing],
            "alternatives": list(self.alternatives),
            "status": self.status,
        }


class CapabilityRefusal(RuntimeError):
    """Raised when the dataset cannot answer the parsed request."""

    def __init__(self, report: CapabilityReport):
        super().__init__("capability refused")
        self.report = report


def _metric_name(metric: Any) -> str:
    return str(metric.raw.get("name_zh") or metric.id)


def _dim_name(sem: Any, dim_id: str) -> str:
    dim = sem.dimensions.get(dim_id)
    if dim is None:
        return dim_id
    return str(dim.raw.get("name_zh") or dim_id)


def _supported_dimensions(sem: Any, metric: Any) -> list[str]:
    return [
        dim_id for dim_id in allowed_dimensions(sem, metric.id)
        if dim_id not in {"none", "month"} and dim_id in sem.dimensions
    ]


def _alternatives(sem: Any, metric: Any | None, dims: list[str],
                  limit: int = 4) -> list[dict]:
    """Deterministic, executable reformulations - never invented by a model."""
    out: list[dict] = []
    seen: set[str] = set()
    if metric is not None:
        for dim_id in _supported_dimensions(sem, metric)[:2]:
            question = f"按「{_dim_name(sem, dim_id)}」查看「{_metric_name(metric)}」"
            if question in seen:
                continue
            seen.add(question)
            out.append({"question": question, "why": "该指标声明支持这个维度"})
        if metric.raw.get("time_via"):
            question = f"查看「{_metric_name(metric)}」按月的走势"
            if question not in seen:
                seen.add(question)
                out.append({"question": question, "why": "该指标有时间列"})
    for candidate in sem.metrics.values():
        if metric is not None and candidate.id == metric.id:
            continue
        if not candidate.raw.get("selected", True):
            continue
        if dims and not set(dims).issubset(set(_supported_dimensions(sem, candidate))):
            continue
        question = f"查看「{_metric_name(candidate)}」"
        if question in seen:
            continue
        seen.add(question)
        out.append({"question": question, "why": "该指标在语义层已发布"})
        if len(out) >= limit:
            break
    return out[:limit]


def _time_overlap(slots: Any, sem: Any, missing: list[Missing],
                  degradations: list[dict], coverage=None) -> None:
    time = getattr(slots, "time", None)
    if time is None:
        return
    try:
        start, end = str(time.start), str(time.end)
        lo, hi = coverage or sem.time_range
    except Exception:
        return
    if not lo or not hi:
        return
    if start >= hi or end <= lo:
        missing.append(Missing(
            "time", f"{start} ~ {end}",
            f"数据只覆盖 {lo} ~ {hi}，这个时间窗内没有任何数据",
        ))
    elif start < lo or end > hi:
        degradations.append({
            "kind": "time",
            "action": "clamp",
            "reason": f"数据只覆盖 {lo} ~ {hi}，超出部分会被忽略",
            "range": [max(start, lo), min(end, hi)],
        })


def assess_slots(slots: Any, sem: Any, policy: Any | None = None,
                 model: Any | None = None) -> CapabilityReport:
    """Assess whether the parsed slots can be answered by this dataset."""
    intent = str(getattr(slots, "intent", "query") or "query")
    metric_id = getattr(slots, "metric", None)
    dims = [d for d in (getattr(slots, "dimensions", []) or []) if d and d != "none"]
    filters = dict(getattr(slots, "filters", {}) or {})

    if intent == "lookup":
        report = CapabilityReport(status="answerable")
        if dims and dims[0] not in sem.dimensions:
            report.missing.append(Missing("dimension", dims[0], "语义层没有这个维度"))
            report.status = "refusable"
        report.alternatives = _alternatives(sem, None, [])
        return report

    report = CapabilityReport(status="answerable", metric=metric_id)
    metric = None
    metric_expr = getattr(slots, "metric_expr", None)
    if metric_expr:
        from app.domain.query.metric import build_metric_spec

        try:
            spec = build_metric_spec(
                metric_expr, sem, label=(metric_expr or {}).get("label")
            )
            metric = Metric(spec["id"], spec)
            report.metric = metric.id
        except Exception as exc:
            report.missing.append(Missing("metric", "派生指标", str(exc)))
    elif not metric_id:
        report.missing.append(Missing("metric", "指标", "没有识别出要计算的指标"))
    elif metric_id not in sem.metrics:
        report.missing.append(Missing("metric", str(metric_id), "语义层里没有这个指标"))
    else:
        metric = sem.metrics[metric_id]

    capability = build_capabilities(sem).get(metric.id) if metric is not None else None
    if capability is not None and capability.time_axis:
        report.notes.append(f"时间口径：{capability.time_axis}")

    if metric is not None and metric.type == "ratio":
        numerator = " ".join(
            str(metric.raw.get(key) or "")
            for key in ("numerator", "grain_numerator", "expr")
        )
        compact = re.sub(r"\s+", "", numerator)
        for dim_id in filters:
            dim = sem.dimensions.get(dim_id)
            if dim is None or dim.kind != "derived":
                continue
            predicate = re.sub(r"\s+", "", str(dim.raw.get("expr") or ""))
            if predicate and predicate in compact:
                report.missing.append(Missing(
                    "filter", _dim_name(sem, dim_id),
                    "比率的分子已经包含这个判定条件，再按它过滤会让分母也变成分子"
                    "（结果恒为 100%）；请去掉该过滤，或改用分组",
                    code="RATIO_SELF_FILTER",
                ))

    if metric is not None and model is not None:
        from app.domain.model.validate import validate_semantics

        for issue in validate_semantics(model):
            if issue.object_id == metric.id and issue.severity == "error":
                report.missing.append(Missing(
                    "metric", _metric_name(metric), issue.message,
                    code=issue.code,
                ))

    ratio_denominator_id = getattr(slots, "ratio_denominator", None)
    if metric is not None and ratio_denominator_id:
        try:
            denominator = sem.metric(str(ratio_denominator_id))
        except Exception:
            report.missing.append(Missing(
                "metric", str(ratio_denominator_id),
                "比率的分母指标不存在",
                code="MISSING_METRIC",
            ))
        else:
            numerator = sem.metric(
                str(getattr(slots, "ratio_numerator", None) or metric.id)
            )
            if numerator.fact != denominator.fact:
                report.missing.append(Missing(
                    "metric", f"{numerator.name} / {denominator.name}",
                    "分子和分母来自不同事实表，需要先对齐 grain/join",
                    code="METRIC_RATIO_FACT_MISMATCH",
                ))
            elif (numerator.raw.get("scope") or {}) != (denominator.raw.get("scope") or {}):
                report.missing.append(Missing(
                    "metric", f"{numerator.name} / {denominator.name}",
                    "分子和分母的 scope 不一致",
                    code="METRIC_RATIO_SCOPE_MISMATCH",
                ))
            elif (numerator.raw.get("time_via") or "") != (denominator.raw.get("time_via") or ""):
                report.missing.append(Missing(
                    "metric", f"{numerator.name} / {denominator.name}",
                    "分子和分母的时间轴不一致",
                    code="METRIC_RATIO_TIME_MISMATCH",
                ))

    if metric is not None and policy is not None:
        decision = policy.check(metric.id, tuple(dims))
        if not decision.allowed:
            report.missing.append(Missing(
                "policy", _metric_name(metric),
                decision.reason or "当前策略不允许访问该指标",
                code=decision.code or "ACCESS_DENIED",
            ))

    if metric is not None:
        supported = (
            set(capability.allowed_dimensions) | {"none"}
            if capability is not None
            else set(metric.raw.get("supported_grain") or ["none"])
        )
        kept: list[str] = []
        for dim_id in dims:
            if dim_id not in sem.dimensions:
                report.degradations.append({
                    "kind": "dimension", "action": "drop", "dimension": dim_id,
                    "reason": "语义层没有这个维度",
                })
                continue
            if dim_id not in supported:
                fanout = (
                    capability is not None
                    and dim_id in capability.fanout_dimensions
                )
                if fanout:
                    report.missing.append(Missing(
                        "dimension",
                        _dim_name(sem, dim_id),
                        "该维度需要经过一对多连接，会放大行数；"
                        "不能安全地按它聚合",
                        code="FANOUT_UNSAFE",
                    ))
                    continue
                report.degradations.append({
                    "kind": "dimension", "action": "drop", "dimension": dim_id,
                    "reason": f"指标「{_metric_name(metric)}」不支持这个维度",
                })
                continue
            kept.append(dim_id)
        if len(kept) != len(dims):
            report.notes.append("部分维度无法拆分，已降级为可用维度或整体口径")

    for dim_id in filters:
        if dim_id not in sem.dimensions:
            report.missing.append(Missing(
                "filter", str(dim_id), "过滤条件引用了语义层不存在的维度",
            ))
        elif metric is not None and sem.dimensions[dim_id].kind == "categorical":
            if not (sem.dimensions[dim_id].raw.get("filter_column")
                    or sem.dimensions[dim_id].raw.get("column")):
                report.missing.append(Missing(
                    "filter", _dim_name(sem, dim_id), "该维度不能作为过滤条件",
                ))

    health = metric.raw.get("data_health") if metric is not None else None
    coverage = (
        capability.time_coverage
        if capability is not None and capability.time_coverage
        else (health.get("time_coverage") if isinstance(health, dict) else None)
    )

    if metric is not None:
        scope = metric.raw.get("scope") or {}
        for dim_id, spec in filters.items():
            dim = sem.dimensions.get(dim_id)
            if dim is None:
                continue
            ref = str(dim.raw.get("filter_column") or dim.raw.get("column") or "")
            if ref not in scope:
                continue
            allowed = str(scope[ref])
            values = (
                spec.value if isinstance(spec.value, (list, tuple, set))
                else [spec.value]
            )
            texts = [str(value) for value in values]
            if spec.op in {"eq", "in"} and any(text != allowed for text in texts):
                report.missing.append(Missing(
                    "filter", _dim_name(sem, dim_id),
                    f"指标口径已限定 {ref}={allowed}，与筛选值 {texts} 冲突",
                ))

    time_axis = getattr(slots, "time_axis", None)
    if time_axis and metric is not None:
        if not time_axis_allowed(sem, metric, str(time_axis)):
            report.missing.append(Missing(
                "time", str(time_axis),
                "该时间轴不是此指标可达的时间列",
            ))
        elif str(time_axis) != str(metric.raw.get("time_via") or ""):
            axis_coverage = next(
                (
                    tuple(axis["coverage"])
                    for axis in (getattr(sem, "time_axes", None) or [])
                    if axis.get("ref") == str(time_axis) and axis.get("coverage")
                ),
                None,
            )
            coverage = axis_coverage or (
                tuple(sem.time_range) if sem.time_range else None
            )

    if metric is not None and getattr(slots, "time", None) is not None:
        if not (metric.raw.get("time_via") or time_axis):
            report.missing.append(Missing(
                "time", "时间范围",
                f"指标「{_metric_name(metric)}」没有时间列，不能按时间筛选",
            ))
        else:
            _time_overlap(
                slots, sem, report.missing, report.degradations, coverage,
            )

    if isinstance(health, dict):
        report.data_health.append(health)
        if health.get("rows") == 0:
            report.missing.append(Missing(
                "data", _metric_name(metric), "该指标对应的数据表没有记录",
            ))
        if health.get("measure_null_rate") == 1:
            report.missing.append(Missing(
                "data", _metric_name(metric), "该指标的度量列全部为空",
            ))

    if report.missing:
        report.status = "refusable"
    elif report.degradations:
        report.status = "degradable"

    report.alternatives = _alternatives(sem, metric, dims)
    return report


def apply_degradations(slots: Any, report: CapabilityReport) -> Any:
    """Return a copy of the slots with unsupported dimensions removed."""
    if not report.degradations:
        return slots
    dropped = {
        item.get("dimension") for item in report.degradations
        if item.get("action") == "drop" and item.get("kind") == "dimension"
    }
    clone = slots.model_copy(deep=True) if hasattr(slots, "model_copy") else slots
    if dropped:
        clone.dimensions = [
            d for d in getattr(clone, "dimensions", []) if d not in dropped
        ]
    for item in report.degradations:
        if item.get("kind") != "time" or item.get("action") != "clamp":
            continue
        try:
            start, end = item["range"]
            clone.time = TimeRange(
                start=date.fromisoformat(str(start)),
                end=date.fromisoformat(str(end)),
            )
            clone.notes = list(getattr(clone, "notes", []) or []) + [
                str(item.get("reason") or "时间范围已按数据覆盖裁剪")
            ]
        except Exception:
            continue
    return clone


def render_refusal(report: CapabilityReport, sem: Any) -> str:
    """Render a structured refusal; the model never writes this text."""
    if not report.missing:
        return "这个问题当前数据源无法回答。"
    lines = ["这个问题当前数据源答不了。", ""]
    for item in report.missing[:3]:
        label = {
            "metric": "指标",
            "dimension": "维度",
            "filter": "筛选条件",
            "time": "时间范围",
            "data": "数据",
        }.get(item.kind, "条件")
        lines.append(f"- 缺少{label}「{item.name}」：{item.reason}")
    available_metrics = [
        _metric_name(m) for m in sem.metrics.values() if m.raw.get("selected", True)
    ][:6]
    if available_metrics:
        lines.append("")
        lines.append("可用指标：" + "、".join(available_metrics))
    lo, hi = getattr(sem, "time_range", ("", ""))
    if lo and hi:
        lines.append(f"数据时间范围：{lo} ~ {hi}")
    if report.alternatives:
        lines.append("")
        lines.append("可以这样问：")
        lines.extend(f"- {item['question']}" for item in report.alternatives[:3])
    return "\n".join(lines).strip()
