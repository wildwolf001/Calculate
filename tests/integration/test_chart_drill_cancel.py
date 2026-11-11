"""Chart recommendation and deterministic drill/cancel boundaries."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.datasets.registry import DatasetRegistry
from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import Metric, SemanticLayer
from app.domain.visualization import recommend_chart
from app.llm.compose_answer import TemplateComposer

DATASET_ID = "ds_dd0addc20f5c"
DATASET = Path(__file__).resolve().parents[2] / "data" / "datasets" / DATASET_ID


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="fixture",
        tables={"t": {"grain": ["c01"], "time": "c04", "columns": ["c01", "c03", "c04"]}},
        joins={}, pre_aggregates={},
        metrics={
            "m": Metric("m", {
                "name_zh": "销售额", "type": "aggregate", "fact": "t",
                "expr": "SUM(t.c03)", "unit": "元",
            }),
            "ratio": Metric("ratio", {
                "name_zh": "延迟率", "type": "ratio", "fact": "t",
                "numerator": "1", "denominator": "1", "unit": "%",
            }),
        },
        dimensions={
            "month": {"name_zh": "月份", "kind": "time", "column": "t.c04"},
            "cat": {"name_zh": "品类", "kind": "categorical", "column": "t.c03"},
        },
        policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, capabilities={}, raw={},
    )


def _result(columns, rows) -> ResultSet:
    return ResultSet(
        result_id="r", columns=columns, column_types=["VARCHAR"] * len(columns),
        rows=rows, row_count=len(rows), truncated=False, sql="", params=[],
        fingerprint="fp", duration_ms=1, meta={},
    )


def _display(columns):
    return {"columns": [
        {"key": key, "label": key, "type": "metric" if key == "value" else "text"}
        for key in columns
    ]}


def test_single_value_recommends_kpi_even_if_llm_says_bar():
    plan = QueryPlan(metric="m", visualization={"type": "bar"})
    spec = recommend_chart(plan, _sem(), _display(["value"]), _result(["value"], [[10]]))
    assert spec["recommended"] == "kpi"
    assert spec["source"] == "rule"


def test_time_dimension_recommends_line():
    plan = QueryPlan(metric="m", dimensions=["month"])
    spec = recommend_chart(
        plan, _sem(), _display(["month", "value"]),
        _result(["month", "value"], [["2025-01", 1], ["2025-02", 2]]),
    )
    assert spec["recommended"] == "line"
    assert "area" in spec["alternatives"]


def test_many_categories_recommend_horizontal_bar():
    plan = QueryPlan(metric="m", dimensions=["cat"])
    rows = [[f"c{i}", i] for i in range(12)]
    spec = recommend_chart(plan, _sem(), _display(["cat", "value"]), _result(["cat", "value"], rows))
    assert spec["recommended"] == "horizontal_bar"


def test_share_recommends_donut_and_ratio_does_not():
    plan = QueryPlan(metric="m", dimensions=["cat"])
    display = _display(["cat", "value", "share"])
    spec = recommend_chart(
        plan, _sem(), display,
        _result(["cat", "value", "share"], [["a", 1, .4], ["b", 2, .6]]),
    )
    assert spec["recommended"] == "donut"
    ratio_plan = QueryPlan(metric="ratio", dimensions=["cat"])
    ratio = recommend_chart(
        ratio_plan, _sem(), _display(["cat", "value"]),
        _result(["cat", "value"], [["a", .4], ["b", .6]]),
    )
    assert ratio["recommended"] == "horizontal_bar"


class DrillParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=0.9,
            slots={
                "metric": "m_004_c06_sum",
                "dimensions": ["d_004_009_c03"],
                "filters": {},
            },
        )


class NoopParser:
    def parse(self, text, context):
        raise AssertionError("cancel should stop before parser")


@pytest.mark.skipif(not DATASET.exists(), reason="dataset is not available")
def test_drill_filter_returns_a_result_with_chart(tmp_path):
    from app.core.session import SessionService
    ctx = DatasetRegistry().get(DATASET_ID)
    store = EventStore(tmp_path / "events")
    checker = Controller(store, ctx.sem, DrillParser(), RuleGrounder(ctx.sem, ctx.dim_values_path),
                         TemplateComposer(), ctx=ctx)
    service = SessionService(checker, store)

    async def run():
        await service.create("s-drill", DATASET_ID)
        events = [event async for event in service.turn("s-drill", "按品类看商品销售额")]
        return events

    events = asyncio.run(run())
    executed = next(event.payload for event in events if event.type == "sql.executed")
    row = executed["rows"][0]
    dim = executed["plan"]["dimensions"][0]
    drilled = service.drill(
        "s-drill", executed["result_id"], "filter",
        dimension=dim, value=str(row[0]), limit=5,
    )
    assert drilled["rows"]
    assert drilled["plan"]["filters"][dim]["value"] == str(row[0])
    assert drilled["chart"]["recommended"] in {"kpi", "bar", "table"}


@pytest.mark.skipif(not DATASET.exists(), reason="dataset is not available")
def test_cancel_before_turn_emits_terminal_event(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    controller = Controller(
        EventStore(tmp_path / "events"), ctx.sem, NoopParser(),
        RuleGrounder(ctx.sem, ctx.dim_values_path), TemplateComposer(), ctx=ctx,
    )
    controller.cancel("s-cancel")

    async def run():
        return [event async for event in controller.run_turn("s-cancel", "卖了多少")]

    events = asyncio.run(run())
    assert any(event.type == "turn.cancelled" for event in events)
    assert any(event.type == "turn.completed" for event in events)
    assert not any(event.type == "sql.executed" for event in events)
