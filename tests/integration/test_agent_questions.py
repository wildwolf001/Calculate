"""End-to-end: rule parser + real pipeline + template composer (no LLM key).

This is the offline acceptance for M3: the public questions must still be
answered correctly when the model is unavailable.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer
from app.llm.rule_parser import RuleParser

QUESTIONS = {
    "Q01": ("2024年1月至2025年12月共有多少笔已送达订单？", 10714),
    "Q02": ("2024年1月至2025年12月已送达订单的商品销售额是多少？不含运费。", 4858920.84),
    "Q04": ("4月商品销售额最高的品类是什么？金额是多少？", 105245.50),
    "Q07": ("有多少自然客户完成了至少2笔已送达订单？", 2378),
}


def make(sem, tmp_path):
    store = EventStore(tmp_path)
    parser = RuleParser(sem)
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    return store, ctrl


def run(ctrl, sid, text):
    async def _consume():
        return [ev async for ev in ctrl.run_turn(sid, text)]
    return asyncio.run(_consume())


@pytest.mark.parametrize("qid", list(QUESTIONS))
def test_public_question_offline(qid, sem, tmp_path):
    question, expected = QUESTIONS[qid]
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", question)
    state = store.replay("s1")
    assert state.status.value == "DONE", state.error
    artifact = state.artifacts[state.last_result_id]
    value = artifact["rows"][0][1] if len(artifact["rows"][0]) > 1 else artifact["rows"][0][0]
    assert float(value) == pytest.approx(expected, abs=0.01)
    assert artifact["sql"]
    assert state.messages[-1]["role"] == "assistant"


def test_q03_growth_answer(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", "4月商品销售额较3月增长多少？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    row = state.artifacts[state.last_result_id]["rows"][0]
    assert row[3] == pytest.approx(0.15, abs=0.0001)
    assert "15.00%" in state.messages[-1]["text"]


def test_q05_delay_rate_answer(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", "5月已送达订单不少于10笔的商家中，延迟率最高的是谁？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    rows = state.artifacts[state.last_result_id]["rows"]
    assert rows[0][0] == "广州美妆个护商家004"
    assert rows[0][3] == pytest.approx(0.9153, abs=0.0001)
    answer = state.messages[-1]["text"]
    assert "91.53%" in answer or "0.92" in answer


def test_q06_review_answer_mentions_coverage(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", "有评价的已送达订单中，延迟订单与准时订单的平均评分分别是多少？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert "2.00" in state.messages[-1]["text"]
    assert "4.02" in state.messages[-1]["text"]
    assert "覆盖" in state.messages[-1]["text"] or "评价" in state.messages[-1]["text"]


def test_q08_explain_without_sql(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    events = run(ctrl, "s1", "直接关联商品明细和支付表为什么会导致金额重复？")
    types = [e.type for e in events]
    assert "sql.compiled" not in types
    assert "answer.composed" in types
    assert "订单" in store.replay("s1").messages[-1]["text"]


def test_follow_up_inherits_and_changes_time(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", "4月商品销售额最高的品类是什么？")
    first_plan = store.replay("s1").plan
    run(ctrl, "s1", "那5月呢？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    plan = state.plan
    assert plan.metric == "gmv"
    assert plan.dimensions == first_plan.dimensions == ["category"]
    assert plan.time.start == date(2025, 5, 1)


def test_follow_up_change_dimension(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", "4月销售额是多少？")
    run(ctrl, "s1", "按商家拆")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    assert state.plan.dimensions == ["seller"]
    assert state.plan.time.start == date(2025, 4, 1)


def test_filter_value_is_grounded_to_id(sem, tmp_path):
    store, ctrl = make(sem, tmp_path)
    run(ctrl, "s1", "数码家电4月的销售额是多少？")
    state = store.replay("s1")
    assert state.status.value == "DONE"
    value = state.slots.filters["category"].value
    assert value == ["electronics"] or value == "electronics"
    assert state.bindings and state.bindings[0]["values"] == ["数码家电"]
