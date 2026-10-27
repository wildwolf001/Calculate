"""Deterministic conversation-context compilation and scope patching."""
from __future__ import annotations

import json

from app.core.context import apply_context_action, build_conversation_context
from app.core.interfaces import ParseResult
from app.core.state import SessionState, Slots
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _semantic(tmp_path) -> SemanticLayer:
    values = tmp_path / "dim_values.json"
    values.write_text(json.dumps({
        "values": {"t_products.c02": ["坚果礼盒", "巧克力"]},
        "id_maps": {},
        "aliases": {},
    }, ensure_ascii=False), encoding="utf-8")
    return SemanticLayer(
        version=1,
        dataset="销售",
        tables={"t_items": {"grain": ["c01"]}, "t_products": {"grain": ["c01"]}},
        joins={
            "t_items": {
                "t_products": {"left": "c02", "right": "c01", "card": "N:1"},
            }
        },
        pre_aggregates={},
        metrics={
            "seller_count": Metric("seller_count", {
                "name_zh": "商家数",
                "type": "aggregate",
                "fact": "t_items",
                "expr": "COUNT(DISTINCT t_items.c04)",
                "supported_grain": ["none"],
            })
        },
        dimensions={
            "product": Dimension("product", {
                "name_zh": "商品名称",
                "column": "t_products.c02",
                "filter_lookup": "t_products.c02",
                "join_path": ["t_items -> t_products"],
            }),
            "seller": Dimension("seller", {
                "name_zh": "商家名称",
                "column": "t_sellers.c02",
                "filter_lookup": "t_sellers.c02",
            }),
        },
        policies={},
        time_range=("2025-01-01", "2025-07-01"),
        binding={"tables": {}},
        capabilities={},
        raw={},
    )


def _state() -> SessionState:
    artifact = {
        "result_id": "r1",
        "columns": ["value"],
        "rows": [[2]],
        "row_count": 1,
        "truncated": False,
        "plan": {
            "kind": "aggregate",
            "metric": "seller_count",
            "dimensions": [],
            "filters": {
                "product": {"op": "eq", "value": "坚果礼盒"},
            },
            "time": None,
            "limit": 1,
        },
    }
    return SessionState(
        session_id="s1",
        turn=2,
        last_result_id="r1",
        artifacts={"r1": artifact},
        slots=Slots(metric="seller_count"),
        messages=[
            {"role": "user", "text": "坚果礼盒的商家数是多少？", "seq": 1},
            {"role": "assistant", "text": "2 个商家", "seq": 2},
        ],
    )


def test_context_compiler_extracts_active_scope_and_entity_focus(tmp_path):
    ctx = build_conversation_context(_state(), _semantic(tmp_path))
    assert ctx["active_result_id"] == "r1"
    assert ctx["active_scope"]["filters"]["product"]["value"] == "坚果礼盒"
    assert ctx["focus"]["kind"] == "entity_count"
    assert ctx["focus"]["entity_dimension"] == "seller"
    assert ctx["focus"]["expected_count"] == 2


def test_reference_result_turns_count_followup_into_member_lookup(tmp_path):
    context = build_conversation_context(_state(), _semantic(tmp_path))
    parsed = ParseResult(
        intent_type="query",
        slots={"dimensions": [], "filters": {}},
        context_action="reference_result",
        context_source="active_result",
    )
    apply_context_action(parsed, context)
    assert parsed.intent_type == "lookup"
    assert parsed.lookup_dim == "seller"
    assert parsed.slots["filters"]["product"]["value"] == "坚果礼盒"


def test_inherit_preserves_scope_without_lexical_cue(tmp_path):
    context = build_conversation_context(_state(), _semantic(tmp_path))
    parsed = ParseResult(
        intent_type="query",
        slots={"dimensions": []},
        context_action="refine",
    )
    apply_context_action(parsed, context)
    assert parsed.slots["metric"] == "seller_count"
    assert parsed.slots["filters"]["product"]["value"] == "坚果礼盒"
