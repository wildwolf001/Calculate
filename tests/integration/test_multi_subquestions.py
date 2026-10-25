"""Acceptance: one user message may carry several independent questions.

Previously the parser's `subquestions` were only logged: the controller took
the first one's slots and silently dropped the rest. Now every sub-question
gets its own plan/SQL/checks cycle and the answers are merged in ask order.
"""
from __future__ import annotations

import asyncio

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer

CMP = {
    "label": "2025年4月 vs 2025年3月",
    "current": {"start": "2025-04-01", "end": "2025-05-01"},
    "baseline": {"start": "2025-03-01", "end": "2025-04-01"},
}


def three_questions() -> list[dict]:
    return [
        {
            "question": "2025年4月已送达订单的商品销售额比3月变化多少？",
            "intent_type": "query",
            "slots": {"metric": "gmv", "dimensions": [], "comparison": dict(CMP)},
        },
        {
            "question": "哪个品类对增长贡献最大？",
            "intent_type": "query",
            "slots": {
                "metric": "gmv",
                "dimensions": ["category"],
                "comparison": dict(CMP),
                "order_by": [{"field": "growth_rate", "direction": "desc"}],
            },
        },
        {
            "question": "能否确定增长是促销带来的？",
            "intent_type": "unsupported",
            "explanation": "数据中没有促销字段，无法做因果归因。",
        },
    ]


class ScriptedParser:
    def __init__(self, subs: list[dict], intent_type: str = "query"):
        self.subs = subs
        self.intent_type = intent_type

    def parse(self, text, context):
        return ParseResult(intent_type=self.intent_type, tier="B", confidence=0.9,
                           slots={}, subquestions=list(self.subs))


async def _consume(ctrl, session_id, text):
    return [ev async for ev in ctrl.run_turn(session_id, text)]


def run(parser, tmp_path, sem, text):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    return store, asyncio.run(_consume(ctrl, "s1", text))


def test_all_questions_execute_in_ask_order(tmp_path, sem):
    store, events = run(ScriptedParser(three_questions()), tmp_path, sem,
                        "4月比3月变化多少？哪个品类贡献最大？能否确定是促销带来的？")

    executed = [e for e in events if e.type == "sql.executed"]
    assert [e.payload["interpretation_id"] for e in executed] == ["sq1", "sq2"]
    assert [e.payload["preferred"] for e in executed] == [True, False]
    assert executed[0].payload["subquestion_id"] == "sq1"

    # Comparison + dimension is not an additive-share plan: no 'value' crash.
    sq2_checks = next(
        e for e in events
        if e.type == "checks.completed" and e.payload.get("interpretation_id") == "sq2"
    )
    assert not [i for i in sq2_checks.payload["issues"] if i["id"] == "SHARE_CHECK_FAILED"]

    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "你这次一共问了 3 个问题" in answer
    assert "**1." in answer and "**2." in answer and "**3." in answer
    assert "促销" in answer
    assert "数码家电" in answer

    state = store.replay("s1")
    assert state.status is Status.DONE
    assert len({e.payload["result_id"] for e in executed}) == 2
    assert len(state.messages) == 2  # user message + one merged answer
    assert state.last_result_id == executed[0].payload["result_id"]
    # The first executed sub-question becomes the next turn's inheritance base.
    assert state.slots.metric == "gmv"
    assert state.slots.comparison is not None


def test_one_bad_subquestion_does_not_cancel_the_rest(tmp_path, sem):
    parser = ScriptedParser([
        {"question": "利润是多少？", "intent_type": "query",
         "slots": {"metric": "profit"}},
        {"question": "3月订单数是多少？", "intent_type": "query",
         "slots": {"metric": "order_count",
                   "time": {"start": "2025-03-01", "end": "2025-04-01"}}},
    ])
    store, events = run(parser, tmp_path, sem, "利润是多少？3月订单数是多少？")

    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 1
    assert executed[0].payload["interpretation_id"] == "sq2"
    assert any(e.type == "subquestion.rejected" for e in events)

    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "无法回答" in answer and "订单数" in answer
    assert store.replay("s1").status is Status.DONE


def test_mixed_intent_container_still_runs_subquestions(tmp_path, sem):
    """Top-level intent may say "unsupported"; the sub-questions still execute."""
    subs = three_questions()[:2]
    store, events = run(ScriptedParser(subs, intent_type="unsupported"), tmp_path, sem,
                        "4月比3月变化多少？哪个品类贡献最大？")
    assert len([e for e in events if e.type == "sql.executed"]) == 2
    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "你这次一共问了 2 个问题" in answer
    assert store.replay("s1").status is Status.DONE


def test_lookup_and_explain_subquestions_need_no_sql(tmp_path, sem):
    parser = ScriptedParser([
        {"question": "有哪些品类？", "intent_type": "lookup",
         "slots": {}, "lookup_dimension": "category"},
        {"question": "商品销售额怎么算？", "intent_type": "explain",
         "explanation": "商品销售额 = 已送达订单的 price 合计，不含运费。"},
    ])
    store, events = run(parser, tmp_path, sem, "有哪些品类？销售额怎么算？")

    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 1  # only the lookup produced SQL
    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "品类" in answer and "price 合计" in answer
    assert store.replay("s1").status is Status.DONE
