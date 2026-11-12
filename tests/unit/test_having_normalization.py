"""User-written thresholds must bind to real aggregate fields, never be dropped."""
from __future__ import annotations

from app.core.controller import Controller
from app.domain.planner import plan_from_ir
from app.domain.query.ir import QueryIR
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="test",
        tables={"orders": {"grain": ["order_id"], "time": "date", "columns": ["order_id", "status", "date", "seller"]}},
        joins={},
        pre_aggregates={},
        metrics={
            "delay_rate": Metric("delay_rate", {
                "name_zh": "延迟率",
                "type": "ratio",
                "fact": "orders",
                "ratio_grain": "fact",
                "ratio_key": "orders.order_id",
                "grain_numerator": "MAX(CASE WHEN orders.late = 1 THEN 1 ELSE 0 END)",
                "grain_denominator": "1",
                "scope": {"orders.status": "delivered"},
                "time_via": "orders.date",
            }),
        },
        dimensions={
            "seller": Dimension("seller", {
                "name_zh": "商家", "kind": "categorical", "column": "orders.seller",
            }),
        },
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        capabilities={},
        raw={},
    )


def test_descriptive_having_metric_binds_to_order_count():
    plan = plan_from_ir(QueryIR(
        metric="delay_rate",
        dimensions=["seller"],
        having=[{
            "metric": "m_llm_已送达订单数",
            "op": "gte",
            "value": 3,
        }],
    ), _sem())
    assert plan.having[0].metric == "order_count"
    assert plan.having[0].required is True


def test_explicit_optional_having_remains_droppable():
    plan = plan_from_ir(QueryIR(
        metric="delay_rate",
        dimensions=["seller"],
        having=[{
            "metric": "m_llm_已送达订单数",
            "op": "gte",
            "value": 3,
            "required": False,
        }],
    ), _sem())
    assert plan.having[0].required is False


def test_chinese_numeric_sample_threshold_is_recovered_from_text():
    controller = object.__new__(Controller)
    controller.sem = _sem()
    slots = {"metric": "delay_rate", "dimensions": ["seller"]}
    controller._apply_sample_threshold(
        slots, "2025年6月延迟率最高的十个商家（至少三笔已送达订单）"
    )
    assert slots["having"] == [{
        "metric": "order_count", "op": "gte", "value": 3, "required": True,
    }]
