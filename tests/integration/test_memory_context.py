"""Semantic memory compression: durable event, soft projection, deterministic fallback."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.memory import MemoryCompressor
from app.datasets.registry import DatasetRegistry
from app.domain.plan import QueryPlan
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer

DATASET_ID = "ds_d7390c8574a4"
DATASET = Path(__file__).resolve().parents[2] / "data" / "datasets" / DATASET_ID

pytestmark = pytest.mark.skipif(
    not DATASET.exists(), reason="memory dataset is not available",
)


def _metric(sem):
    return next(
        m for m in sem.metrics.values()
        if m.type == "aggregate" and m.raw.get("selected", True)
    )


class OneQueryParser:
    def __init__(self, metric_id):
        self.metric_id = metric_id

    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="A",
            confidence=1.0,
            slots={"metric": self.metric_id, "dimensions": [], "limit": 100},
        )


def test_deterministic_memory_has_summary_and_scope():
    ctx = DatasetRegistry().get(DATASET_ID)
    sem = ctx.sem
    metric = _metric(sem)
    plan = QueryPlan(metric=metric.id, dimensions=[], limit=100)
    artifact = {"rows": [[10]], "columns": ["value"], "row_count": 1}
    memory = MemoryCompressor(sem=sem).compress(
        previous=None,
        question="销售额是多少？",
        answer="销售额是 10。",
        plan=plan,
        artifact=artifact,
        recent_messages=[],
        turn=2,
    )
    assert memory["source"] == "deterministic"
    assert memory["scope_digest"]["metric"] == metric.id
    assert memory["summary"]
    assert memory["entities"] == []


def test_memory_compressed_event_is_appended_and_projected(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    sem = ctx.sem
    metric = _metric(sem)
    store = EventStore(tmp_path)
    controller = Controller(
        store,
        sem,
        OneQueryParser(metric.id),
        RuleGrounder(sem, ctx.dim_values_path),
        TemplateComposer(),
        ctx=ctx,
        memory_compressor=MemoryCompressor(sem=sem),
    )

    async def run():
        return [
            event async for event in controller.run_turn(
                "m1", f"{metric.name}是多少？"
            )
        ]

    events = asyncio.run(run())
    stored = store.load("m1")
    assert any(e.type == "memory.compressed" for e in stored)
    state = store.replay("m1")
    assert state.memory
    assert state.memory["summary"]
    assert state.memory["turn"] == 1
