from __future__ import annotations

from app.domain.semantic.graph import build_capabilities, fanout_safe
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.domain.semantic.timeaxis import infer_time_axis


def _sem(metric: Metric) -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="t",
        tables={
            "t": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02"]},
            "p": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
            "c": {"grain": ["c01"], "time": None, "columns": ["c01"]},
        },
        joins={
            "p": {"t": {"left": "c01", "right": "c01", "card": "N:1"}},
            "t": {"c": {"left": "c02", "right": "c01", "card": "N:1"}},
        },
        pre_aggregates={},
        metrics={"m": metric},
        dimensions={
            "d_parent": Dimension("d_parent", {
                "id": "d_parent", "name_zh": "客户", "column": "c.c01",
            }),
            "d_child": Dimension("d_child", {
                "id": "d_child", "name_zh": "支付方式", "column": "p.c01",
            }),
            "d_flag": Dimension("d_flag", {
                "id": "d_flag", "name_zh": "是否延迟", "kind": "derived",
                "requires": "t", "labels": {"true": "延迟", "false": "准时"},
            }),
        },
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def _sum_metric() -> Metric:
    return Metric("m", {
        "name_zh": "金额", "type": "aggregate", "fact": "t",
        "expr": "SUM(t.c01)", "time_via": "t.c02",
    })


def test_graph_separates_safe_and_fanout_dimensions():
    capability = build_capabilities(_sem(_sum_metric()))["m"]
    assert "d_parent" in capability.dimensions
    assert "d_flag" in capability.dimensions
    assert "d_child" in capability.fanout_dimensions
    assert "d_child" not in capability.allowed_dimensions


def test_distinct_metric_allows_fanout_dimension():
    metric = Metric("m", {
        "name_zh": "订单数", "type": "aggregate", "fact": "t",
        "expr": "COUNT(DISTINCT t.c01)",
    })
    capability = build_capabilities(_sem(metric))["m"]
    assert "d_child" in capability.allowed_dimensions
    assert capability.time_axis is None


def test_fanout_safe_rule():
    assert fanout_safe(_sum_metric()) is False

    distinct = Metric("d", {
        "name_zh": "订单数", "type": "aggregate", "fact": "t",
        "expr": "COUNT(DISTINCT t.c01)",
    })
    assert fanout_safe(distinct) is True
    assert infer_time_axis("purchase_at") == "下单时间"
    assert infer_time_axis("review_created_at") == "评价时间"
