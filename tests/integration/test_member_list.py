"""Member lists are a plan shape, not a separate executor: scope must survive.

Regression for "食品饮料下面有哪些？" returning all 600 products because the
old lookup executor had no WHERE clause.
"""
from __future__ import annotations

import asyncio

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer


class ScopedLookupParser:
    def __init__(self, dim: str, filters: dict | None = None, time: dict | None = None):
        self.dim = dim
        self.filters = filters or {}
        self.time = time

    def parse(self, text, context):
        slots = {"filters": self.filters}
        if self.time:
            slots["time"] = self.time
        return ParseResult(
            intent_type="lookup", tier="C", confidence=0.9,
            lookup_dim=self.dim, slots=slots,
            rewritten_question=text,
        )


def run(tmp_path, sem, parser, text):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    async def _consume():
        return [ev async for ev in ctrl.run_turn("s1", text)]
    events = asyncio.run(_consume())
    return store, events


def _executed(events):
    return [e.payload for e in events if e.type == "sql.executed"]


def test_scoped_member_list_keeps_category_filter(tmp_path, sem):
    parser = ScopedLookupParser(
        "product", {"category": {"op": "eq", "value": "食品饮料"}}
    )
    store, events = run(tmp_path, sem, parser, "食品饮料下面有哪些？")
    payload = _executed(events)[0]
    assert payload["row_count"] == 100
    assert payload["plan"]["kind"] == "member_list"
    assert payload["plan"]["list_dim"] == "product"
    assert payload["plan"]["filters"]["category"]["value"] == ["food"]
    assert "categories" in payload["sql"].lower()
    assert payload["params"][-1] == "food"

    state = store.replay("s1")
    assert state.status is Status.DONE
    names = {r[0] for r in payload["rows"]}
    assert "坚果礼盒12款" in names
    assert "机械键盘11款" not in names  # category filter was dropped before


def test_member_list_with_time_only_returns_active_members(tmp_path, sem):
    parser = ScopedLookupParser(
        "product", time={"start": "2025-05-01", "end": "2025-06-01"}
    )
    _store, events = run(tmp_path, sem, parser, "2025年5月卖过哪些商品？")
    payload = _executed(events)[0]
    assert payload["plan"]["kind"] == "member_list"
    assert 0 < payload["row_count"] < 600
    assert all(r[1] > 0 for r in payload["rows"])
    assert "2025-05-01" in payload["params"]


def test_member_list_answers_with_scope_line(tmp_path, sem):
    parser = ScopedLookupParser(
        "product", {"category": {"op": "eq", "value": "食品饮料"}}
    )
    _store, events = run(tmp_path, sem, parser, "食品饮料下面有哪些？")
    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "品类=食品饮料" in answer
    assert "100 个商品" in answer
