from __future__ import annotations

import json

from app.core.interfaces import ParseResult
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.client import LLMClient
from app.llm.parse_intent import LLMIntentParser


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="ratio_fixture",
        tables={"orders": {"grain": ["order_id"], "time": "order_date",
                             "columns": ["order_id", "seller", "status", "order_date"]}},
        joins={}, pre_aggregates={},
        metrics={
            "delay_rate": Metric("delay_rate", {
                "name_zh": "延迟交付率", "type": "ratio", "fact": "orders",
                "numerator": "COUNT(CASE WHEN orders.delay > 0 THEN 1 END)",
                "denominator": "COUNT(DISTINCT orders.order_id)",
                "grain_numerator": "MAX(CASE WHEN orders.delay > 0 THEN 1 ELSE 0 END)",
                "grain_denominator": "1",
                "ratio_grain": "fact", "ratio_key": "orders.order_id",
                "scope": {"orders.status": "delivered"},
                "time_via": "orders.order_date",
                "supported_grain": ["none", "seller"],
                "synonyms": ["延迟交付率", "延迟率"],
            }),
            "delay_orders": Metric("delay_orders", {
                "name_zh": "延迟订单", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(CASE WHEN orders.delay > 0 THEN 1 END)",
                "scope": {"orders.status": "delivered"},
                "time_via": "orders.order_date",
                "supported_grain": ["none", "seller"],
            }),
            "order_count": Metric("order_count", {
                "name_zh": "订单数", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(DISTINCT orders.order_id)",
                "scope": {"orders.status": "delivered"},
                "time_via": "orders.order_date",
                "supported_grain": ["none", "seller"],
            }),
        },
        dimensions={"seller": Dimension("seller", {
            "name_zh": "商家名称", "kind": "categorical", "column": "orders.seller",
        })},
        policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, capabilities={"ratio": True},
        raw={}, time_axes=[],
    )


def test_interpretation_prefers_published_ratio(tmp_path):
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text(json.dumps({"values": {}, "aliases": {}}), encoding="utf-8")
    parser = LLMIntentParser(
        _sem(), client=LLMClient(api_key=""), dim_values_path=dim_values,
        index={}, use_brief=False,
    )
    result = ParseResult(
        intent_type="query",
        slots={"metric": "delay_rate"},
        interpretations=[{
            "id": "delayed_rate", "confidence": 0.8,
            "slots": {
                "metric": "delay_orders",
                "ratio_numerator": "delay_orders",
                "ratio_denominator": "order_count",
                "dimensions": ["seller"],
                "having": [{"metric": "order_count", "op": "gte", "value": 3}],
            },
        }],
        preferred_interpretation="delayed_rate",
    )
    out = parser._postprocess_interpretations(
        "5月已送达订单不少于3笔的商家中，延迟率最高的是谁？", result, {
            "slots": {},
        },
    )
    slots = out.interpretations[0]["slots"]
    assert slots["metric"] == "delay_rate"
    assert slots["ratio_numerator"] is None
    assert slots["ratio_denominator"] is None
    assert slots["dimensions"] == ["seller"]
