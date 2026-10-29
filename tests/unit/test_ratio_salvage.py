from __future__ import annotations

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.domain.query.ir import QueryIR
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="t",
        tables={"t": {"grain": ["c01"], "time": None, "columns": ["c01", "c02", "c04"]}},
        joins={}, pre_aggregates={},
        metrics={
            "amount": Metric("amount", {
                "name_zh": "金额", "type": "aggregate", "fact": "t",
                "expr": "SUM(t.c04)", "synonyms": ["销售额"],
            }),
            "orders": Metric("orders", {
                "name_zh": "订单数", "type": "aggregate", "fact": "t",
                "expr": "COUNT(DISTINCT t.c01)",
            }),
        },
        dimensions={"d": Dimension("d", {"id": "d", "name_zh": "维度", "column": "t.c02"})},
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def _controller(tmp_path) -> Controller:
    return Controller(EventStore(tmp_path / "sessions"), _sem(), None, None, None)


def test_ratio_syntax_salvage(tmp_path):
    controller = _controller(tmp_path)
    slots = {"metric": None, "dimensions": [], "filters": {}, "notes": []}
    controller._salvage_ratio_expression("帮我算一下金额除以订单数", slots)
    assert slots["ratio_numerator"] == "amount"
    assert slots["ratio_denominator"] == "orders"
    assert slots["metric"] == "amount"


def test_per_metric_syntax_salvage(tmp_path):
    controller = _controller(tmp_path)
    slots = {"metric": None, "dimensions": [], "filters": {}, "notes": []}
    controller._salvage_ratio_expression("每订单数的金额", slots)
    assert slots["ratio_numerator"] == "amount"
    assert slots["ratio_denominator"] == "orders"


def test_undefined_business_name_is_not_salvaged(tmp_path):
    controller = _controller(tmp_path)
    slots = {"metric": None, "dimensions": [], "filters": {}, "notes": []}
    controller._salvage_ratio_expression("客单价是多少", slots)
    assert slots.get("ratio_denominator") is None


def test_query_ir_carries_ratio_fields():
    ir = QueryIR.from_slots({
        "metric": "amount",
        "ratio_numerator": "amount",
        "ratio_denominator": "orders",
        "dimensions": [],
    })
    assert ir.ratio_numerator == "amount"
    assert ir.ratio_denominator == "orders"
