"""Regression tests for the issues found in the first manual review."""
from __future__ import annotations

import asyncio

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.domain.rewrite import rewrite
from app.domain.resolver import RuleGrounder
from app.llm.client import LLMTruncated
from app.llm.compose_answer import LLMComposer, TemplateComposer
from app.llm.parse_intent import LLMIntentParser
from app.llm.rule_parser import RuleParser


def make(store, sem, parser=None):
    return Controller(store, sem, parser or RuleParser(sem), RuleGrounder(sem), TemplateComposer())


def run(ctrl, sid, text):
    async def _consume():
        return [ev async for ev in ctrl.run_turn(sid, text)]
    return asyncio.run(_consume())


def test_rewrite_smalltalk_and_lookup():
    assert rewrite("你好").kind == "smalltalk"
    assert rewrite("您好呀").kind == "smalltalk"
    r = rewrite("商品种类一共有哪些？")
    assert r.kind == "lookup" and r.lookup_dim == "category"
    r = rewrite("有哪些商家")
    assert r.kind == "lookup" and r.lookup_dim == "seller"
    assert rewrite("哪些品类的销售额最高").kind is None
    assert rewrite("请问　4月的销售额").normalized.startswith("4月")


def test_greeting_is_terminal_not_clarification(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "你好")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert "明账" in state.messages[-1]["text"]
    assert not any(e.type == "ambiguity.detected" for e in store.load("s1"))


def test_lookup_categories_end_to_end(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    events = run(ctrl, "s1", "商品种类一共有哪些？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    artifact = state.artifacts[state.last_result_id]
    assert artifact["row_count"] == 6
    assert artifact["columns"] == ["value", "order_count"]
    shown = [c["key"] for c in artifact["display"]["columns"] if c["show"]]
    assert shown == ["value", "order_count"]
    assert any(e.type == "sql.executed" for e in events)


def test_q05_display_hides_internal_order_count(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "5月已送达订单不少于10笔的商家中，延迟率最高的是谁？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    artifact = state.artifacts[state.last_result_id]
    shown = [c["key"] for c in artifact["display"]["columns"] if c["show"]]
    labels = {c["key"]: c["label"] for c in artifact["display"]["columns"]}
    assert shown == ["seller", "numerator", "denominator", "value"]
    assert labels["numerator"] == "延迟订单数"
    assert labels["denominator"] == "已送达订单数"
    assert "order_count" not in shown


def test_followup_switches_dimension_without_appending(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "5月已送达订单不少于10笔的商家中，延迟率最高的是谁？")
    run(ctrl, "s1", "只看广州美妆个护商家004，按品类拆")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert state.plan.dimensions == ["category"]
    assert state.slots.filters["seller"].value in ("广州美妆个护商家004", ["S004"])
    artifact = state.artifacts[state.last_result_id]
    assert artifact["rows"][0][0] == "美妆个护"


def test_fresh_question_does_not_inherit_time(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "5月已送达订单不少于10笔的商家中，延迟率最高的是谁？")
    run(ctrl, "s1", "有评价的已送达订单中，延迟订单与准时订单的平均评分分别是多少？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert str(state.plan.time.start) == "2024-01-01"
    scores = {r[0]: r[1] for r in state.artifacts[state.last_result_id]["rows"]}
    assert scores["延迟"] == pytest.approx(2.00, abs=0.01)


def test_unsupported_question_gets_visible_answer(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    events = run(ctrl, "s1", "毛利率是多少？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert any(e.type == "answer.composed" for e in events)
    assert "成本" in state.messages[-1]["text"] or "无法" in state.messages[-1]["text"]


class BadMetricParser:
    def parse(self, text, context):
        return ParseResult(intent_type="query", tier="A", confidence=0.9,
                           slots={"metric": "not_a_metric"})


def test_plan_failure_is_visible(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem, parser=BadMetricParser())
    events = run(ctrl, "s1", "随便问一个")
    types = [e.type for e in events]
    assert "plan.rejected" in types
    assert "error.raised" in types
    assert store.replay("s1").status.value == "FAILED"


class TruncatingClient:
    available = True

    def complete_text(self, *args, **kwargs):
        raise LLMTruncated("stop_reason=max_tokens")

    def complete_json(self, *args, **kwargs):
        raise LLMTruncated("stop_reason=max_tokens")


def test_composer_falls_back_when_truncated():
    ctx = {
        "columns": ["value"], "rows": [[4858920.84]], "row_count": 1,
        "time": {"start": "2024-01-01", "end": "2025-12-26"},
        "metric_name": "商品销售额", "metric_unit": "元", "metric_type": "aggregate",
        "dimensions": [], "dimension_labels": [], "checks": [], "facts": {},
    }
    text = LLMComposer(client=TruncatingClient()).compose(ctx)
    assert "4,858,920.84" in text


def test_parser_falls_back_when_truncated(sem):
    parser = LLMIntentParser(sem, client=TruncatingClient())
    result = parser.parse("2024年1月至2025年12月的销售额", {"slots": {}})
    assert result.slots["metric"] == "gmv"
    assert any("规则解析" in n for n in result.notes)


def test_product_values_and_stems_are_ingested():
    import json
    from app.config.settings import DIM_VALUES_PATH
    data = json.loads(DIM_VALUES_PATH.read_text(encoding="utf-8"))
    assert len(data["values"]["products.product_name"]) == 600
    assert "机械键盘" in data["product_stems"]
    assert len(data["product_stems"]["机械键盘"]) == 20


def test_family_binding_expands_keyboard(sem):
    from app.domain.resolver import RuleGrounder
    result = RuleGrounder(sem).ground(
        {"filters": {"product": {"op": "eq", "value": "键盘"}}}, [], {}
    )
    assert result.ambiguity is None
    binding = result.bindings[0]
    assert binding["match"] == "family"
    assert len(binding["ids"]) == 20


def test_keyboard_kinds_and_units_offline(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "键盘一共有多少种，每种买了多少")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert state.plan.metric == "item_count"
    assert state.plan.dimensions == ["product"]
    artifact = state.artifacts[state.last_result_id]
    assert artifact["row_count"] == 20
    assert sum(row[1] for row in artifact["rows"]) == 676
    labels = {c["key"]: c["label"] for c in artifact["display"]["columns"]}
    assert labels["product"] == "商品"
    assert labels["value"] == "商品件数（件）"
    assert "20" in state.messages[-1]["text"] or "20 组" in state.messages[-1]["text"]


def test_keyboard_product_count_offline(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "键盘一共有多少种")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert state.plan.metric == "product_count"
    assert state.artifacts[state.last_result_id]["rows"][0][0] == 20


def test_unknown_product_is_not_silently_dropped(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = make(store, sem)
    run(ctrl, "s1", "牛奶一共买了多少钱")
    state = store.replay("s1")
    assert state.status.value == "AWAITING_CLARIFICATION"
    assert state.pending["term"] == "牛奶"
