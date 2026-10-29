from __future__ import annotations

import pytest

from app.llm.client import LLMClient, extract_json
from app.llm.compose_answer import numbers_are_grounded
from app.llm.parse_intent import LLMIntentParser
from app.llm.rule_parser import RuleParser


def test_extract_json_from_fence_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('说明文字 {"a": 2} 结尾') == {"a": 2}
    with pytest.raises(Exception):
        extract_json("没有 JSON")


def test_number_whitelist_accepts_grounded_numbers():
    ctx = {
        "rows": [["数码家电", 105245.5]],
        "columns": ["category", "value"],
        "row_count": 1,
        "checks": [{"title": "覆盖率", "detail": "86.17%"}],
        "time": {"start": "2025-04-01", "end": "2025-05-01"},
    }
    assert numbers_are_grounded("数码家电销售额为 105,245.50 元，增长 15.00%。", ctx)
    assert numbers_are_grounded("覆盖约 86.17%。", ctx)


def test_number_whitelist_rejects_invented_number():
    ctx = {"rows": [["数码家电", 105245.5]], "row_count": 1,
           "time": {"start": "2025-04-01", "end": "2025-05-01"}}
    assert not numbers_are_grounded("数码家电销售额为 120,000.00 元。", ctx)


def test_llm_layer_never_executes_sql():
    """SQL is produced by the compiler only; the LLM layer must stay text-only."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "llm"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "duckdb" in text or "sqlglot" in text or ".execute(" in text:
            offenders.append(path.name)
    assert offenders == [], f"LLM 层不应直接接触数据库或 SQL 执行: {offenders}"


def test_fallback_sql_module_is_gone():
    with pytest.raises(ImportError):
        __import__("app.llm.fallback_sql")


def test_llm_parser_falls_back_when_unavailable(sem):
    parser = LLMIntentParser(sem, client=LLMClient(api_key=""))
    result = parser.parse("2024年1月至2025年12月的销售额", {"slots": {}})
    assert result.slots["metric"] == "gmv"
    assert any("离线" in n for n in result.notes)


def test_rule_parser_q05_slots(sem):
    parser = RuleParser(sem)
    r = parser.parse("5月已送达订单不少于10笔的商家中，延迟率最高的是谁？", {"slots": {}})
    assert r.slots["metric"] == "delay_rate"
    assert r.slots["dimensions"] == ["seller"]
    assert r.slots["having"][0] == {"metric": "order_count", "op": "gte", "value": 10}
    assert r.slots["time"]["start"] == "2025-05-01"


def test_rule_parser_explain_q08(sem):
    parser = RuleParser(sem)
    r = parser.parse("直接关联商品明细和支付表为什么会导致金额重复？", {"slots": {}})
    assert r.intent_type == "explain"
    assert "聚合" in r.explanation
