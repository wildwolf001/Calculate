from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.compose_answer import TemplateComposer


class CoverageParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="coverage", tier="A", confidence=1.0,
            slots={"metric": "row_count", "intent": "coverage"},
        )


def test_coverage_without_time_answers_with_quality_summary(tmp_path: Path):
    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table orders(order_id varchar, status varchar)")
    con.execute("insert into orders values ('O1','delivered')")
    con.close()
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text(json.dumps({"values": {"orders.status": ["delivered"]}}),
                          encoding="utf-8")
    sem = SemanticLayer(
        version=1, dataset="broken",
        tables={"orders": {"grain": ["order_id"], "time": None,
                             "columns": ["order_id", "status"]}},
        joins={}, pre_aggregates={},
        metrics={"row_count": Metric("row_count", {
            "name_zh": "记录数", "type": "aggregate", "fact": "orders",
            "expr": "COUNT(*)", "supported_grain": ["none"],
        })},
        dimensions={"status": Dimension("status", {
            "name_zh": "状态", "kind": "categorical", "column": "orders.status",
        })},
        policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, capabilities={}, parser="generic",
        raw={}, time_axes=[],
    )
    ctx = SimpleNamespace(
        sem=sem, warehouse=db, dim_values_path=dim_values,
        quality={"checks": [
            {"id": "no_unique_key", "severity": "warn",
             "title": "部分表没有唯一主键", "count": 1},
            {"id": "duplicate_full_rows", "severity": "warn",
             "title": "存在重复整行", "count": 2},
        ]},
        dataset_id="broken", mode="published",
    )
    ctrl = Controller(EventStore(tmp_path / "events"), sem, CoverageParser(),
                      RuleGrounder(sem, dim_values), TemplateComposer(), ctx=ctx)
    async def run():
        return [event async for event in ctrl.run_turn("s1", "这份数据质量怎么样？")]
    events = asyncio.run(run())
    text = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "没有可用于时间覆盖诊断的时间列" in text
    assert "部分表没有唯一主键" in text
    assert "存在重复整行" in text
    assert not any(e.type == "error.raised" for e in events)
