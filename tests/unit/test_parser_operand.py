"""Logical-name resolution for parser-generated derived metrics."""
from __future__ import annotations

import json

from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.client import LLMClient
from app.llm.parse_intent import LLMIntentParser


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="fixture",
        tables={
            "orders": {
                "grain": ["order_id"], "time": "order_date",
                "columns": ["order_id", "state", "order_date", "amount"],
            },
            "items": {
                "grain": ["item_id"], "time": None,
                "columns": ["item_id", "order_id", "state"],
            },
        },
        joins={"items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "total": Metric("total", {
                "name_zh": "总额", "type": "aggregate", "fact": "orders",
                "expr": "SUM(orders.amount)",
            }),
        },
        dimensions={
            "d_state_orders": Dimension("d_state_orders", {
                "name_zh": "订单状态", "kind": "categorical", "column": "orders.state",
            }),
        },
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        capabilities={},
        raw={},
        time_axes=[],
    )


def _parser(tmp_path, index: dict) -> LLMIntentParser:
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text(json.dumps({"values": {}, "aliases": {}}), encoding="utf-8")
    return LLMIntentParser(
        _sem(), client=LLMClient(api_key=""), dim_values_path=dim_values,
        index=index, use_brief=False,
    )


def test_metric_operand_resolves_logical_column_name(tmp_path):
    index = {
        "tables": {
            "items": {"columns": [
                {"name": "c01", "label": "review_score", "type": "integer"},
            ]},
        },
    }
    parser = _parser(tmp_path, index)
    assert parser._resolve_operand_ref("reviews.review_score") == "items.c01"
    spec = parser._resolve_metric_expr({
        "aggregation": "AVG", "operand": "reviews.review_score", "fact": "reviews",
    })
    assert spec["operand"] == "items.c01"
    assert spec["fact"] == "items"


def test_ambiguous_column_label_is_not_guessed(tmp_path):
    index = {
        "tables": {
            "items": {"columns": [{"name": "c01", "label": "score"}]},
            "orders": {"columns": [{"name": "c09", "label": "score"}]},
        },
    }
    parser = _parser(tmp_path, index)
    assert parser._resolve_operand_ref("x.score") == "x.score"
