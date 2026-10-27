"""Dataset-aware analysis planning without a warehouse dependency."""
from __future__ import annotations

from datetime import date

from app.domain.analysis import (
    build_analysis_items,
    custom_analysis_item,
    module_catalog,
)
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.domain.pipeline import prepare
from app.domain.plan import ComparisonSpec, QueryPlan, TimeRange


def _semantic() -> SemanticLayer:
    metrics = {
        "sales": Metric("sales", {
            "name_zh": "销售额",
            "type": "aggregate",
            "fact": "orders",
            "expr": "SUM(amount)",
            "additive": True,
            "time_via": "orders.order_date",
            "supported_grain": ["none", "month", "category", "seller"],
            "selected": True,
        }),
        "orders": Metric("orders", {
            "name_zh": "订单数",
            "type": "aggregate",
            "fact": "orders",
            "expr": "COUNT(DISTINCT order_id)",
            "additive": False,
            "time_via": "orders.order_date",
            "supported_grain": ["none", "month", "category"],
            "selected": True,
        }),
        "delay": Metric("delay", {
            "name_zh": "延迟率",
            "type": "ratio",
            "fact": "orders",
            "ratio_key": "orders.order_id",
            "grain_numerator": "SUM(late)",
            "grain_denominator": "COUNT(*)",
            "time_via": "orders.order_date",
            "supported_grain": ["none", "seller"],
            "selected": True,
        }),
    }
    dimensions = {
        "month": Dimension("month", {"name_zh": "月份", "kind": "time"}),
        "category": Dimension("category", {"name_zh": "品类"}),
        "seller": Dimension("seller", {"name_zh": "商家"}),
    }
    return SemanticLayer(
        version=3,
        dataset="合成销售",
        tables={"orders": {"grain": ["order_id"], "time": "order_date"}},
        joins={},
        pre_aggregates={},
        metrics=metrics,
        dimensions=dimensions,
        policies={},
        time_range=("2025-01-01", "2025-07-01"),
        binding={"tables": {"orders": {}}},
        capabilities={"ratio": True},
        parser="generic",
        raw={"compiler_version": 2},
    )


def test_module_catalog_uses_supported_metrics_and_dimensions():
    sem = _semantic()
    modules = {item["id"] for item in module_catalog(sem)}
    assert {"overview", "trend", "structure", "comparison", "fulfillment"} <= modules
    assert "payment" not in modules
    assert "customer" not in modules


def test_selected_modules_and_custom_block_are_independent():
    sem = _semantic()
    items, window = build_analysis_items(sem, modules=["overview"])
    assert items and {item.module for item in items} == {"overview"}
    assert window["current"]["start"] == "2025-06-01"

    custom = custom_analysis_item(
        {"metric": "sales", "dimensions": ["category"]}, "自定义品类分析"
    )
    assert custom.module == "custom"
    assert custom.slots["limit"] == 10
    assert custom.section == "自定义分析"


def test_focus_filters_apply_only_to_compatible_items():
    sem = _semantic()
    items, _ = build_analysis_items(
        sem,
        focus={
            "filters": {"category": {"op": "eq", "value": "办公"}},
            "anchor_end": "2025-04-30",
        },
        modules=["overview", "structure"],
    )
    by_module = {item.module: item for item in items}
    assert by_module["overview"].slots["filters"]["category"]["value"] == "办公"
    assert "filters" not in next(
        item.slots for item in items
        if item.module == "structure" and not item.apply_filters
    )


def test_ratio_metric_supports_explicit_period_comparison():
    sem = _semantic()
    plan = QueryPlan(
        metric="delay",
        comparison=ComparisonSpec(
            current=TimeRange(
                start=date(2025, 4, 1), end=date(2025, 5, 1)
            ),
            baseline=TimeRange(
                start=date(2025, 3, 1), end=date(2025, 4, 1)
            ),
        ),
        limit=5,
    )
    compiled, issues = prepare(plan, sem)
    assert not [issue for issue in issues if issue.severity == "error"]
    assert '"current_num"' in compiled.sql
    assert '"baseline_den"' in compiled.sql
    assert compiled.columns == [
        "current", "baseline", "delta", "growth_rate",
    ]
