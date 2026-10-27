"""Presentation contract for a result set.

The compiler's columns are for machine correctness; the UI should show
business columns. This module decides labels/visibility once, on the server,
so the canvas never has to guess (and never leaks internal columns such as
`order_count` when it duplicates `denominator`).
"""
from __future__ import annotations

from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer


def display_spec(plan: QueryPlan, sem: SemanticLayer, result: ResultSet) -> dict:
    if plan.kind == "member_list":
        dim = sem.dimension(plan.list_dim or "")
        label = dim.raw.get("name_zh", plan.list_dim)
        columns = [{"key": "value", "label": label, "type": "text", "show": True}]
        if "order_count" in result.columns:
            coverage = sem.capabilities.get("member_list_coverage") or {}
            columns.append({
                "key": "order_count",
                "label": coverage.get("label") or "覆盖数",
                "type": "int",
                "show": True,
            })
        return {
            "columns": columns,
            "metric_name": f"{label}清单",
            "unit": None,
        }

    metric = sem.metric(plan.metric)
    unit = metric.raw.get("unit")
    unit_suffix = f"（{unit}）" if unit else ""
    present = set(result.columns)
    cols: list[dict] = []

    def add(key: str, label: str, kind: str, show: bool = True):
        if key in present:
            cols.append({"key": key, "label": label, "type": kind, "show": show})

    for dim in plan.dimensions:
        add(dim, sem.dimension(dim).raw.get("name_zh", dim), "text")

    if plan.compare or plan.comparison:
        add("current", "本期", "metric")
        add("baseline", "基期", "metric")
        add("delta", "变化", "delta")
        add("growth_rate", "增长率", "percent")
    elif metric.type == "ratio":
        add("numerator", metric.raw.get("numerator_label", "分子"), "int")
        add("denominator", metric.raw.get("denominator_label", "分母"), "int")
        add("value", metric.name + unit_suffix, "percent" if unit == "%" else "metric")
        add("order_count", "订单数", "int", show=False)
    else:
        add("value", metric.name + unit_suffix, "percent" if unit == "%" else "metric")

    return {"columns": cols, "metric_name": metric.name, "unit": unit}
