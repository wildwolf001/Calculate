"""Deterministic chart recommendation for a query result.

The parser may suggest a chart type, but a type is only usable when it matches
the result shape. This module is the rule-based authority; the LLM suggestion is
kept only when it is compatible, so the canvas never falls back to the same
handful of charts for every question.
"""
from __future__ import annotations

from typing import Any

from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer

CHART_TYPES = (
    "kpi", "line", "area", "bar", "horizontal_bar", "grouped_bar",
    "stacked_bar", "donut", "pie", "scatter", "heatmap", "waterfall", "table",
)

LLM_ALIASES = {
    "grouped_bar": "grouped_bar",
    "bar": "bar",
    "line": "line",
    "pie": "donut",
    "kpi": "kpi",
    "table": "table",
}


def _is_time_dim(plan: QueryPlan, sem: SemanticLayer, dim: str) -> bool:
    if dim == "month":
        return True
    dimension = sem.dimensions.get(dim)
    raw = getattr(dimension, "raw", dimension) or {}
    return str(raw.get("kind") or "") == "time"


def _numeric_columns(display: dict[str, Any], columns: list[str]) -> list[str]:
    types = {
        item.get("key"): item.get("type")
        for item in (display.get("columns") or [])
        if item.get("key")
    }
    return [
        key for key in columns
        if key in {"value", "current", "baseline", "numerator", "denominator"}
        or types.get(key) in {"metric", "percent", "int", "delta"}
    ]


def recommend_chart(plan: QueryPlan, sem: SemanticLayer,
                    display: dict[str, Any], result: ResultSet) -> dict[str, Any]:
    """Return a chart contract: recommendation + compatible alternatives."""
    columns = list(result.columns or [])
    rows = list(result.rows or [])
    dimensions = list(plan.dimensions or [])
    compare = bool(plan.compare or plan.comparison)
    row_count = int(result.row_count or len(rows))
    metric_type = ""
    if plan.metric and plan.metric in sem.metrics:
        metric_type = str(sem.metrics[plan.metric].type or "")
    has_share = "share" in columns
    has_growth = "growth_rate" in columns or "delta" in columns
    time_dims = [dim for dim in dimensions if _is_time_dim(plan, sem, dim)]
    cat_dims = [dim for dim in dimensions if dim not in time_dims]
    numeric = _numeric_columns(display, columns)
    source = "rule"
    hint = None
    if plan.visualization and plan.visualization.get("type"):
        candidate = LLM_ALIASES.get(str(plan.visualization["type"]))
        if candidate:
            hint = candidate
            source = "llm"

    def spec(recommended: str, alternatives: list[str], reason: str,
             x: str | None = None, y: str | None = None,
             series: str | None = None) -> dict[str, Any]:
        # The rule owns the recommendation. A model hint may confirm it, but
        # must not silently replace a KPI with a one-bar chart (or vice versa):
        # the reason would then describe a different chart than the one shown.
        return {
            "recommended": recommended,
            "alternatives": list(dict.fromkeys([recommended, *alternatives])),
            "reason": reason,
            "source": "llm" if hint == recommended else "rule",
            "x": x,
            "y": y or ("current" if compare and "current" in columns else "value"),
            "series": series,
        }

    # A single scalar is a KPI, not a bar chart with one bar.
    if not dimensions:
        if row_count <= 1:
            return spec("kpi", ["table"], "单值结果用 KPI 卡片，避免一根孤立的柱子。")
        return spec("table", ["bar"], "没有分组维度，表格比图形更可读。")

    if plan.kind == "member_list":
        return spec("table", ["bar"], "维度成员清单适合表格浏览。")

    if compare:
        if len(cat_dims) == 1 and has_growth:
            return spec(
                "grouped_bar", ["waterfall", "bar", "table"],
                "本期/基期对比用分组柱；若要看贡献拆解可切瀑布图。",
                x=cat_dims[0], series="period",
            )
        return spec("grouped_bar", ["bar", "table"],
                    "对比结果包含本期、基期两列，分组柱状图最直观。",
                    x=dimensions[0] if dimensions else None, series="period")

    if len(dimensions) == 1 and time_dims:
        return spec(
            "line", ["area", "bar", "table"],
            "时间维度优先折线/面积，连续趋势比分类柱子更合适。",
            x=dimensions[0],
        )

    if len(dimensions) == 1:
        dim = dimensions[0]
        if has_share and row_count <= 8 and metric_type != "ratio":
            return spec(
                "donut", ["bar", "horizontal_bar", "table"],
                "结果含占比，环形图适合看部分与整体的关系。",
                x=dim,
            )
        if metric_type == "ratio":
            return spec(
                "horizontal_bar", ["bar", "table"],
                "比率结果按分类排序，横向条形比饼图更易比较。",
                x=dim,
            )
        if row_count == 1:
            return spec("kpi", ["bar", "table"],
                        "只有一个分组值，用 KPI 突出结果。", x=dim)
        if row_count > 8:
            return spec(
                "horizontal_bar", ["bar", "donut", "table"],
                "分类超过 8 个，横向条形更适合展示排名和长标签。",
                x=dim,
            )
        return spec(
            "bar", ["horizontal_bar", "donut", "table"],
            "分类数量适中、需要比较大小，纵向柱状图最清晰。",
            x=dim,
        )

    # Two or more dimensions.
    if len(numeric) >= 2 and cat_dims and not time_dims:
        return spec(
            "scatter", ["bar", "heatmap", "table"],
            "结果包含两个数值字段，散点图可以观察相关关系。",
            x=numeric[0], y=numeric[1], series=cat_dims[0],
        )
    if time_dims and cat_dims:
        return spec(
            "grouped_bar", ["stacked_bar", "heatmap", "line", "table"],
            "时间 × 分类用分组柱/堆叠柱；分类很多时可切热力图。",
            x=time_dims[0], series=cat_dims[0],
        )
    if row_count > 12:
        return spec(
            "heatmap", ["grouped_bar", "stacked_bar", "table"],
            "两个分类维度的组合较多，热力图更适合发现密集区域。",
            x=dimensions[0], series=dimensions[1],
        )
    return spec(
        "grouped_bar", ["stacked_bar", "heatmap", "bar", "table"],
        "两个分类维度用分组柱/堆叠柱展示组合差异；组合较密时可切热力图。",
        x=dimensions[0], series=dimensions[1],
    )
