from __future__ import annotations

from app.agent.contracts import ToolContext
from app.chat.agent.tools import DescribeMetricTool, ProfileColumnTool, SearchSemanticsTool
from app.core.interfaces import ParseResult
from app.domain.semantic.loader import Metric, SemanticLayer
from app.llm.parse_intent import LLMIntentParser

INDEX = {
    "metrics": {
        "m_refund": {
            "name": "退单数量",
            "type": "aggregate",
            "expr": "COUNT(DISTINCT order_id)",
            "definition": "状态为退单的订单数",
            "supported_dimensions": ["d_channel"],
        },
        "m_orders": {
            "name": "成交单数",
            "type": "aggregate",
            "expr": "COUNT(DISTINCT order_id)",
            "definition": "状态为完成的订单数",
        },
    },
    "dimensions": {
        "d_channel": {"name": "渠道", "kind": "categorical", "values": ["线上", "线下"]},
    },
    "tables": {
        "orders": {
            "rows": 6,
            "grain": ["order_id"],
            "columns": [
                {"name": "order_id", "type": "VARCHAR", "null_rate": 0.0, "samples": ["O1"]},
            ],
        }
    },
}


def _ctx():
    return ToolContext(data={"index": INDEX})


def test_search_finds_refund_metric():
    result = SearchSemanticsTool().run({"query": "退单"}, _ctx())
    assert result.ok
    ids = [item["id"] for item in result.facts["metrics"]]
    assert "m_refund" in ids


def test_describe_returns_expression():
    result = DescribeMetricTool().run({"metric_id": "m_refund"}, _ctx())
    assert result.ok
    assert result.facts["metric"]["expr"] == "COUNT(DISTINCT order_id)"


def test_parser_uses_search_tool_to_rescue_unresolved(tmp_path):
    from app.domain.semantic.loader import SemanticLayer

    sem = SemanticLayer(
        version=1,
        dataset="t",
        tables={"orders": {"grain": ["c01"], "time": None, "columns": ["c01"]}},
        joins={},
        pre_aggregates={},
        metrics={},
        dimensions={},
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        raw={},
    )
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    parser = LLMIntentParser(
        sem, client=None, dim_values_path=dim_values, index=INDEX, use_brief=True,
    )
    result = ParseResult(
        intent_type="query", tier="A", confidence=0.5,
        slots={"unresolved": ["退单数量"]},
    )
    assert parser._has_semantic_hit("退单数量是多少") is True
    assert parser._has_semantic_hit("今天天气怎么样") is False

    result = parser._apply_semantic_tools("退单数量是多少", result)
    assert result.slots["metric"] == "m_refund"
    assert result.slots["metric_source"] == "documented"
    assert result.unresolved == []


def test_prefer_published_ratio_over_scope_mismatch(tmp_path):
    sem = SemanticLayer(
        version=1, dataset="ratio",
        tables={"orders": {"grain": ["c01"], "time": "c04", "columns": ["c01", "c04"]}},
        joins={}, pre_aggregates={},
        metrics={
            "m_delay": Metric("m_delay", {
                "name_zh": "配送延迟率", "type": "ratio", "fact": "orders",
                "numerator": "COUNT(CASE WHEN orders.c04 > orders.c04 THEN 1 END)",
                "denominator": "COUNT(orders.c01)",
                "scope": {"orders.c01": "delivered"},
                "synonyms": ["配送延迟率", "延迟送达率"],
            }),
        },
        dimensions={}, policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    parser = LLMIntentParser(sem, client=None, dim_values_path=dim_values)
    result = ParseResult(
        intent_type="query", tier="A", confidence=0.8,
        slots={"ratio_numerator": "m_late", "ratio_denominator": "m_orders"},
    )
    out = parser._prefer_published_ratio(
        "5月已送达订单不少于3笔的商家中，延迟率最高的是谁？", result,
    )
    assert out.slots["metric"] == "m_delay"
    assert out.slots["ratio_numerator"] is None
    assert out.slots["ratio_denominator"] is None


def test_profile_column_and_unknown():
    result = ProfileColumnTool().run({"table": "orders", "column": "order_id"}, _ctx())
    assert result.ok
    assert result.facts["column"]["type"] == "VARCHAR"
    missing = DescribeMetricTool().run({"metric_id": "nope"}, _ctx())
    assert missing.ok is False
