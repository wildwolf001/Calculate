"""Presentation contract for a result set.

The compiler's columns are for machine correctness; the UI should show
business columns. This module decides labels/visibility once, on the server,
so the canvas never has to guess (and never leaks internal columns such as
`order_count` when it duplicates `denominator`).
"""
from __future__ import annotations

from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import Metric, SemanticLayer


def _metric_for(plan: QueryPlan, sem: SemanticLayer):
    if plan.metric_spec:
        return Metric(plan.metric, plan.metric_spec)
    return sem.metric(plan.metric) if plan.metric else None


def display_spec(plan: QueryPlan, sem: SemanticLayer, result: ResultSet) -> dict:
    if plan.kind == "coverage":
        metric = _metric_for(plan, sem)
        unit = metric.raw.get("unit")
        present = set(result.columns)
        columns = []
        if "month" in present:
            columns.append({"key": "month", "label": "月份", "type": "text", "show": True})
        if "record_count" in present:
            columns.append({
                "key": "record_count", "label": "记录数",
                "type": "int", "show": True,
            })
        if "value" in present:
            columns.append({
                "key": "value", "label": metric.name,
                "type": "percent" if unit == "%" else "metric", "show": True,
            })
        if "is_missing" in present:
            columns.append({
                "key": "is_missing", "label": "缺失",
                "type": "text", "show": True,
            })
        return {
            "columns": columns,
            "metric_name": f"{metric.name}覆盖诊断",
            "unit": unit,
            "provenance": plan.metric_source or "documented",
        }
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
            "provenance": plan.metric_source or "documented",
        }

    metric = _metric_for(plan, sem)
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

    return {
        "columns": cols,
        "metric_name": metric.name,
        "unit": unit,
        "provenance": plan.metric_source or "documented",
    }


def summarize_rows(rows: list, columns: list, dimensions: list[str],
                   max_rows: int = 50) -> dict:
    """Bound what a composer sees, but keep it honest.

    Long results used to be sliced silently, which made the model claim that
    "these five cities are all cities". The summary now carries the true row
    count plus a deterministic per-group top row, so the answer can cover every
    group without shipping hundreds of rows to the model.
    """
    total = len(rows)
    summary = {
        "rows": list(rows[:max_rows]),
        "rows_total": total,
        "rows_truncated": total > max_rows,
        "rows_note": (
            f"结果共 {total} 行，下面只展示前 {min(total, max_rows)} 行；"
            "不要假设未展示的行不存在。"
        ) if total > max_rows else "",
    }
    if total and len(dimensions) >= 2 and columns:
        value_idx = columns.index("value") if "value" in columns else None
        best: dict = {}
        for row in rows:
            key = row[0]
            score = row[value_idx] if value_idx is not None and isinstance(
                row[value_idx], (int, float)
            ) else 0.0
            if key not in best or score > best[key][0]:
                best[key] = (score, list(row))
        top = [row for _score, row in best.values()]
        top.sort(
            key=lambda row: -(
                row[value_idx]
                if value_idx is not None and isinstance(row[value_idx], (int, float))
                else 0.0
            )
        )
        summary["per_group_top"] = top[:20]
        summary["per_group_dimension"] = dimensions[0]
    return summary
