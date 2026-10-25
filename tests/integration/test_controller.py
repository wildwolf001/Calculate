"""M2 acceptance: a full turn with no model, plus replay determinism."""
from __future__ import annotations

import asyncio

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status, reduce
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer

FULL = {"start": "2024-01-01", "end": "2025-12-26"}


class ScriptedParser:
    """Deterministic stand-in for the LLM parser."""

    def __init__(self):
        self.calls = []

    def parse(self, text, context):
        self.calls.append((text, context))
        if "口径" in text:
            return ParseResult(
                intent_type="explain", tier="C", confidence=1.0,
                explanation="商品销售额 = 已送达订单的 price 合计，不含运费。",
            )
        if "销售额" in text:
            return ParseResult(
                intent_type="query", tier="A", confidence=1.0,
                slots={"metric": "gmv", "time": FULL, "dimensions": []},
            )
        if "订单" in text:
            return ParseResult(
                intent_type="query", tier="A", confidence=1.0,
                slots={"metric": "order_count", "time": FULL, "dimensions": []},
            )
        return ParseResult(intent_type="query", tier="A", confidence=0.2,
                           slots={}, unresolved=[text])


def make_controller(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, ScriptedParser(), RuleGrounder(sem), TemplateComposer())
    return store, ctrl


def run(ctrl, session_id, text):
    async def _consume():
        return [ev async for ev in ctrl.run_turn(session_id, text)]
    return asyncio.run(_consume())


def test_full_turn_without_model(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    events = run(ctrl, "s1", "总销售额是多少？")
    types = [e.type for e in events]
    assert types[0] == "turn.started"
    assert "intent.parsed" in types
    assert "values.grounded" in types
    assert "plan.compiled" in types
    assert "sql.compiled" in types
    assert "sql.executed" in types
    assert "checks.completed" in types
    assert "answer.composed" in types
    assert "result.presented" in types
    assert types[-1] == "turn.completed"

    state = store.replay("s1")
    assert state.status is Status.DONE
    artifact = state.artifacts[state.last_result_id]
    assert artifact["rows"][0][0] == pytest.approx(4858920.84, abs=0.01)
    assert "4,858,920.84" in state.messages[-1]["text"]


def test_replay_is_deterministic(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    run(ctrl, "s1", "总销售额是多少？")
    events = store.load("s1")
    a = reduce(events, "s1")
    b = reduce(events, "s1")
    assert a.to_dict() == b.to_dict()
    assert a.last_seq == len(events)


def test_second_turn_gets_new_turn_number(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    run(ctrl, "s1", "总销售额是多少？")
    run(ctrl, "s1", "有多少订单？")
    state = store.replay("s1")
    assert state.turn == 2
    assert state.status is Status.DONE
    artifact = state.artifacts[state.last_result_id]
    assert artifact["rows"][0][0] == 10714
    # both turns remain in history
    assert len(store.history("s1")) == 2


def test_explain_intent_skips_sql(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    events = run(ctrl, "s1", "销售额的口径是什么？")
    types = [e.type for e in events]
    assert "sql.compiled" not in types
    assert "answer.composed" in types
    assert store.replay("s1").status is Status.DONE


def test_unresolved_asks_for_clarification(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    run(ctrl, "s1", "毛利率是多少？")
    state = store.replay("s1")
    assert state.status is Status.AWAITING_CLARIFICATION
    assert state.pending is not None
    assert state.pending["kind"] == "unresolved"


def test_events_are_append_only_with_monotonic_seq(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    run(ctrl, "s1", "总销售额是多少？")
    events = store.load("s1")
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.session_id == "s1" for e in events)


class AmbiguousParser:
    """Always asks about the same unknown category, to exercise clarification."""

    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=0.9,
            slots={
                "metric": "gmv", "time": FULL, "dimensions": [],
                "filters": {"category": {"op": "eq", "value": "珠宝首饰"}},
            },
        )


def test_clarification_roundtrip(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, AmbiguousParser(), RuleGrounder(sem), TemplateComposer())
    events = run(ctrl, "s1", "珠宝首饰的销售额")
    state = store.replay("s1")
    assert state.status is Status.AWAITING_CLARIFICATION
    assert state.pending["kind"] == "value_not_found"
    assert any(e.type == "ambiguity.detected" for e in events)

    # The next message is an answer, not a new question: slots must survive.
    run(ctrl, "s1", "数码家电")
    state = store.replay("s1")
    assert state.status is Status.DONE
    artifact = state.artifacts[state.last_result_id]
    assert artifact["rows"][0][0] > 1_000_000  # digital category, full period
    assert state.slots.metric == "gmv"
    assert state.slots.filters["category"].value in (["electronics"], "electronics")
    assert any(e["role"] == "user" and e["text"] == "数码家电" for e in state.messages)


def test_session_isolation(tmp_path, sem):
    store, ctrl = make_controller(tmp_path, sem)
    run(ctrl, "s1", "总销售额是多少？")
    run(ctrl, "s2", "有多少订单？")
    assert store.replay("s1").turn == 1
    assert store.replay("s2").turn == 1
    assert store.replay("s1").last_result_id != store.replay("s2").last_result_id
