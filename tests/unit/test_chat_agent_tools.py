from __future__ import annotations

from app.agent.contracts import ToolContext
from app.chat.agent.tools import DescribeMetricTool, ProfileColumnTool, SearchSemanticsTool

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


def test_profile_column_and_unknown():
    result = ProfileColumnTool().run({"table": "orders", "column": "order_id"}, _ctx())
    assert result.ok
    assert result.facts["column"]["type"] == "VARCHAR"
    missing = DescribeMetricTool().run({"metric_id": "nope"}, _ctx())
    assert missing.ok is False
