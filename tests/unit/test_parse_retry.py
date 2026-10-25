"""The LLM parser retries once on an invalid payload instead of silently
falling back; and the offline fallback understands enumeration questions."""
from __future__ import annotations

from app.core.interfaces import ParseResult
from app.domain.semantic.loader import load_semantic
from app.llm.parse_intent import IntentParseError, LLMIntentParser

VALID = {
    "intent_type": "lookup",
    "tier": "A",
    "confidence": 0.9,
    "lookup_dimension": "product",
    "slots": {
        "intent": "lookup",
        "dimensions": ["product"],
        "filters": {"category": {"op": "eq", "value": "食品饮料"}},
    },
}


class FakeClient:
    available = True

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def complete_json(self, system, user, max_tokens=2400, stage="parse_intent"):
        self.calls.append(user)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_invalid_output_is_retried_with_feedback(sem):
    client = FakeClient([IntentParseError("未知 lookup_dimension: 具体商品"), VALID])
    parser = LLMIntentParser(sem, client=client)
    result = parser.parse("食品饮料下面有哪些具体商品", {})
    assert result.intent_type == "lookup" and result.lookup_dim == "product"
    assert result.slots["filters"]["category"]["value"] == "食品饮料"
    assert len(client.calls) == 2
    assert "上一次输出未通过校验" in client.calls[1]


def test_two_failures_fall_back_to_rule_parser_with_lookup(sem):
    client = FakeClient([IntentParseError("bad"), IntentParseError("bad")])
    parser = LLMIntentParser(sem, client=client)
    result = parser.parse("食品饮料下面有哪些具体商品", {})
    assert result.intent_type == "lookup" and result.lookup_dim == "product"
    assert result.slots["filters"]["category"]["value"] == "食品饮料"
    assert any("模型解析失败" in n for n in result.notes)


def test_every_merchant_counts_as_explicit_dimension(sem):
    """'每家的延迟率' must keep seller; the follow-up cleanup used to drop it."""
    parser = LLMIntentParser(sem, client=FakeClient([]))
    prev = {
        "slots": {
            "metric": "delay_rate", "dimensions": ["seller"],
            "time": {"start": "2025-05-01", "end": "2025-06-01"},
        }
    }
    result = ParseResult(
        intent_type="query", tier="B", confidence=0.9,
        slots={
            "metric": "delay_rate", "dimensions": ["seller"],
            "time": {"start": "2025-05-01", "end": "2025-06-01"},
        },
    )
    out = parser._postprocess_followup(result, "每家的延迟率都是多少", prev)
    assert out.slots["dimensions"] == ["seller"]
