"""Integration coverage for compound turns, analysis and query verification."""
from __future__ import annotations

import asyncio

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain.analysis import module_catalog
from app.domain.records import verify_record
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer


class ScriptedParser:
    def __init__(self, subquestions: list[dict]):
        self.subquestions = subquestions

    def parse(self, text: str, context: dict) -> ParseResult:
        return ParseResult(
            intent_type="query",
            tier="B",
            confidence=1.0,
            subquestions=self.subquestions,
        )


def _controller(sem, tmp_path, subquestions: list[dict]):
    store = EventStore(tmp_path)
    controller = Controller(
        store,
        sem,
        ScriptedParser(subquestions),
        RuleGrounder(sem),
        TemplateComposer(),
    )
    return store, controller


def _question(metric, question: str) -> dict:
    return {
        "question": question,
        "intent_type": "query",
        "slots": {"metric": metric.id, "dimensions": [], "limit": 1},
    }


def test_all_subquestions_execute_and_failure_is_isolated(sem, tmp_path):
    metrics = [
        metric for metric in sem.metrics.values()
        if metric.raw.get("selected", True) and metric.type == "aggregate"
    ][:2]
    assert len(metrics) == 2
    subquestions = [
        {"question": "利润是多少？", "intent_type": "query",
         "slots": {"metric": "missing_profit"}},
        _question(metrics[0], metrics[0].name + "是多少？"),
        _question(metrics[1], metrics[1].name + "是多少？"),
    ]
    store, controller = _controller(sem, tmp_path, subquestions)

    async def consume():
        return [event async for event in controller.run_turn("s1", "三个问题")]

    events = asyncio.run(consume())
    executed = [event for event in events if event.type == "sql.executed"]
    assert len(executed) == 2
    assert any(event.type == "subquestion.rejected" for event in events)
    assert any(event.type == "subquestions.completed" for event in events)
    assert store.replay("s1").status is Status.DONE


def test_analysis_report_and_query_verification(sem, tmp_path):
    modules = module_catalog(sem)
    assert modules
    store = EventStore(tmp_path)
    controller = Controller(
        store,
        sem,
        ScriptedParser([]),
        RuleGrounder(sem),
        TemplateComposer(),
    )

    async def consume():
        return [
            event async for event in controller.run_analysis(
                "s2", {"sections": [modules[0]["id"]]}
            )
        ]

    events = asyncio.run(consume())
    completed = next(event for event in events if event.type == "analysis.completed")
    assert completed.payload["report"].startswith("# 经营分析报告")
    state = store.replay("s2")
    assert state.status is Status.DONE
    assert state.analysis and state.analysis["report"]

    artifact = next(iter(state.artifacts.values()))
    verified = verify_record(artifact, sem, db_path=controller.warehouse)
    assert verified["verdict"] == "reproduced"
