"""Regression: explicit period-vs-period comparison and multi interpretations.

Both cases used to fail because the IR could only express prev_period/prev_year:
"7月 vs 3月" collapsed to one month, and "3月 vs 2月同比" silently fell back to
year-over-year windows. The parser now decides both ranges explicitly.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer

JUL_VS_MAR = {
    "metric": "item_count",
    "dimensions": [],
    "comparison": {
        "label": "2025年7月 vs 2025年3月",
        "current": {"start": "2025-07-01", "end": "2025-08-01"},
        "baseline": {"start": "2025-03-01", "end": "2025-04-01"},
    },
}
MOM = {
    "metric": "item_count",
    "dimensions": [],
    "comparison": {
        "label": "环比（2025-03 vs 2025-02）",
        "current": {"start": "2025-03-01", "end": "2025-04-01"},
        "baseline": {"start": "2025-02-01", "end": "2025-03-01"},
    },
}
YOY = {
    "metric": "item_count",
    "dimensions": [],
    "comparison": {
        "label": "同比（2025-03 vs 2024-03）",
        "current": {"start": "2025-03-01", "end": "2025-04-01"},
        "baseline": {"start": "2024-03-01", "end": "2024-04-01"},
    },
}


class SingleParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=0.9, slots=dict(JUL_VS_MAR),
            visualization={"type": "grouped_bar", "reason": "两期比较"},
        )


class DualParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=0.8, slots=dict(MOM),
            interpretations=[
                {"id": "yoy", "label": "同比", "confidence": 0.35, "slots": dict(YOY)},
                {"id": "mom", "label": "环比", "confidence": 0.65, "slots": dict(MOM)},
            ],
            preferred_interpretation="mom",
            ambiguity_note="同比与显式月份冲突",
            visualization={"type": "grouped_bar", "reason": "两期比较"},
        )


def run(parser, tmp_path, sem, text):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    events = asyncio.run(_consume(ctrl, "s1", text))
    return store, events


async def _consume(ctrl, session_id, text):
    return [ev async for ev in ctrl.run_turn(session_id, text)]


def test_explicit_comparison_uses_both_periods(tmp_path, sem):
    store, events = run(SingleParser(), tmp_path, sem, "相较于三月份七月份增长的比例是多少")
    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 1
    payload = executed[0].payload
    assert payload["columns"] == ["current", "baseline", "delta", "growth_rate"]
    current, baseline, delta, growth = payload["rows"][0]
    assert current == 844 and baseline == 651 and delta == 193
    assert growth == pytest.approx(193 / 651, rel=1e-3)
    # The model's chart hint survives into the plan so the UI can honour it.
    assert payload["plan"]["comparison"]["current"]["start"] == "2025-07-01"
    assert payload["plan"]["visualization"]["type"] == "grouped_bar"

    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "2025年7月 vs 2025年3月" in answer
    assert store.replay("s1").status is Status.DONE


def test_multi_interpretations_execute_both_and_prefer_one(tmp_path, sem):
    store, events = run(DualParser(), tmp_path, sem, "三月相比于二月同比增长多少")
    executed = [e for e in events if e.type == "sql.executed"]
    assert [e.payload["interpretation_id"] for e in executed] == ["mom", "yoy"]
    assert [e.payload["preferred"] for e in executed] == [True, False]
    for e in executed:
        assert e.payload["plan"]["visualization"]["type"] == "grouped_bar"

    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "环比" in answer and "同比" in answer
    state = store.replay("s1")
    assert state.status is Status.DONE
    # The preferred interpretation is the one surfaced as the turn's result.
    assert state.last_result_id == state.preferred_result_id
    assert "mom" in state.last_result_id
