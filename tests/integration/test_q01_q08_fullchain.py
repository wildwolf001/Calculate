from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import load_semantic
from app.llm.compose_answer import TemplateComposer


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data" / "datasets" / "ds_9e118db001fc"
FIXTURE = ROOT / "tests" / "fixtures" / "q01_q08_ir.json"

pytestmark = pytest.mark.skipif(
    not (DATASET / "warehouse.duckdb").exists()
    or not FIXTURE.exists(),
    reason="competition dataset or recorded IR fixture is not available",
)


class RecordedParser:
    """Replay the IR captured from the live LLM run for Q01-Q08."""

    def __init__(self, data: dict):
        self.data = data

    def parse(self, text: str, context: dict) -> ParseResult:
        item = self.data[text]
        if item["intent_type"] == "explain":
            return ParseResult(
                intent_type="explain", tier="C", confidence=1.0,
                explanation=item.get("explanation") or "",
            )
        return ParseResult(
            intent_type="query", tier="A", confidence=1.0,
            slots=item["ir"],
        )


def _controller(tmp_path: Path):
    sem = load_semantic(
        DATASET / "semantic.yaml", DATASET / "binding.yaml",
        catalog_path=DATASET / "catalog.json",
    )
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    parser = RecordedParser({item["question"]: item for item in data.values()})
    ctrl = Controller(
        EventStore(tmp_path / "events"), sem, parser,
        RuleGrounder(sem, DATASET / "dim_values.json"),
        TemplateComposer(),
    )
    ctrl.warehouse = DATASET / "warehouse.duckdb"
    ctrl.dim_values_path = DATASET / "dim_values.json"
    return ctrl, data


def _turn(ctrl: Controller, session_id: str, question: str):
    async def run():
        return [event async for event in ctrl.run_turn(session_id, question)]
    return asyncio.run(run())


def _rows(events):
    return next(e.payload["rows"] for e in events if e.type == "sql.executed")


def test_q01_q08_fullchain(tmp_path):
    ctrl, data = _controller(tmp_path)
    events = {}
    for qid in [f"Q{i:02d}" for i in range(1, 8)]:
        events[qid] = _turn(ctrl, qid, data[qid]["question"])

    assert _rows(events["Q01"]) == [[159]]
    assert _rows(events["Q02"]) == [[75484.21]]
    q03 = _rows(events["Q03"])[0]
    assert q03[:3] == [14681.34, 12396.58, 2284.76]
    assert q03[3] == pytest.approx(0.184306, abs=1e-4)
    assert _rows(events["Q04"]) == [["数码家电", 8564.6]]
    q05_rows = _rows(events["Q05"])
    assert q05_rows[0] == ["岭南好物", 6, 7, pytest.approx(0.857143, abs=1e-5), 7]
    # Ratio rankings now fetch extra rows for the minimum-sample policy; the
    # first row must stay the adequate-sample winner.
    assert len(q05_rows) >= 1
    q06 = {row[0]: row[1] for row in _rows(events["Q06"])}
    assert q06["延迟"] == pytest.approx(1.833333, abs=1e-4)
    assert q06["准时"] == pytest.approx(4.0, abs=1e-6)
    assert _rows(events["Q07"]) == [[46]]

    q08 = _turn(ctrl, "Q08", data["Q08"]["question"])
    answer = next(e.payload["text"] for e in q08 if e.type == "answer.composed")
    assert "重复" in answer or "fan-out" in answer.lower()
