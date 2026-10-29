from __future__ import annotations

import asyncio

import duckdb

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import SemanticLayer
from app.llm.compose_answer import TemplateComposer


class DerivedParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=1.0,
            slots={
                "metric_expr": {
                    "aggregation": "AVG",
                    "operand": "t1.c04",
                    "fact": "t1",
                    "time_via": "t1.c02",
                    "label": "平均评分",
                },
                "time": {"start": "2025-05-01", "end": "2025-06-01"},
            },
        )


def _sem():
    return SemanticLayer(
        version=1,
        dataset="derived_turn",
        tables={
            "t1": {
                "grain": ["c01"],
                "time": "c02",
                "columns": ["c01", "c02", "c04"],
            }
        },
        joins={},
        pre_aggregates={},
        metrics={},
        dimensions={},
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        raw={},
    )


def _warehouse(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t1 (c01 VARCHAR, c02 DATE, c04 DOUBLE)")
    con.executemany(
        "INSERT INTO t1 VALUES (?, ?, ?)",
        [
            ("o1", "2025-05-01", 2.0),
            ("o2", "2025-05-02", 3.0),
            ("o3", "2025-05-03", 5.0),
            ("o4", "2025-05-04", 4.0),
        ],
    )
    con.close()


def test_controller_turn_with_derived_metric(tmp_path):
    sem = _sem()
    warehouse = tmp_path / "w.duckdb"
    _warehouse(warehouse)
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    ctrl = Controller(
        EventStore(tmp_path / "sessions"), sem, DerivedParser(),
        RuleGrounder(sem, dim_values), TemplateComposer(),
    )
    ctrl.warehouse = warehouse

    async def consume():
        return [event async for event in ctrl.run_turn("s1", "平均评分是多少？")]

    events = asyncio.run(consume())
    types = [event.type for event in events]
    assert "sql.executed" in types
    assert "turn.completed" in types

    state = EventStore(tmp_path / "sessions").replay("s1")
    artifact = state.artifacts[state.last_result_id]
    assert artifact["rows"][0][0] == 3.5
    assert artifact["display"]["provenance"] == "inferred"
