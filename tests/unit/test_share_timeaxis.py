from __future__ import annotations

from datetime import date

import duckdb
import pytest

from app.domain.plan import QueryPlan, TimeRange
from app.domain.pipeline import PipelineError, execute_prepared, prepare
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.domain.validator import validate_plan


def _sem(metric: Metric, time_axes=None) -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="t",
        tables={"t": {"grain": ["c01"], "time": "c02",
                      "columns": ["c01", "c02", "c03", "c04"]}},
        joins={}, pre_aggregates={},
        metrics={"m": metric},
        dimensions={"d_city": Dimension("d_city", {
            "id": "d_city", "name_zh": "城市", "column": "t.c01",
        })},
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
        time_axes=time_axes or [],
    )


def _sum_metric(**extra):
    raw = {
        "name_zh": "销售额", "type": "aggregate", "fact": "t",
        "expr": "SUM(t.c04)", "time_via": "t.c02", "additive": True,
    }
    raw.update(extra)
    return Metric("m", raw)


def _warehouse(tmp_path):
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (c01 VARCHAR, c02 TIMESTAMP, c03 TIMESTAMP, c04 DOUBLE)")
    con.execute(
        "INSERT INTO t VALUES ('A','2025-05-01','2025-05-10',100),"
        "('B','2025-05-02','2025-05-11',50)"
    )
    con.close()
    return path


def test_share_compiles_and_sums_to_one(tmp_path):
    plan = QueryPlan(metric="m", dimensions=["d_city"], share=True)
    compiled, _ = prepare(plan, _sem(_sum_metric()))
    rows = execute_prepared(compiled, db_path=_warehouse(tmp_path)).rows
    assert rows[0][0] == "A"
    assert rows[0][2] == pytest.approx(100 / 150)
    assert rows[1][2] == pytest.approx(50 / 150)


def test_share_requires_dimensions():
    plan = QueryPlan(metric="m", share=True)
    codes = [issue.code for issue in validate_plan(plan, _sem(_sum_metric()))]
    assert "BAD_SHARE" in codes


def test_time_axis_switches_filter_column():
    axes = [
        {"ref": "t.c02", "name_zh": "下单时间", "label": "purchase_at"},
        {"ref": "t.c03", "name_zh": "送达时间", "label": "delivered_at"},
    ]
    plan = QueryPlan(
        metric="m",
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
        time_axis="t.c03",
    )
    compiled, _ = prepare(plan, _sem(_sum_metric(), axes))
    assert "c03" in compiled.sql
    assert "c02" not in compiled.sql


def test_unknown_time_axis_is_rejected():
    plan = QueryPlan(
        metric="m",
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
        time_axis="t.c99",
    )
    codes = [issue.code for issue in validate_plan(plan, _sem(_sum_metric()))]
    assert "UNSUPPORTED_TIME_AXIS" in codes


def test_salvage_time_axis_and_share(tmp_path):
    from app.core.controller import Controller
    from app.core.event_store import EventStore
    from app.core.state import Slots

    axes = [{"ref": "t.c03", "name_zh": "送达时间", "label": "delivered_at"}]
    sem = _sem(_sum_metric(), axes)
    controller = Controller(EventStore(tmp_path / "sessions"), sem, None, None, None)

    payload = {"metric": "m", "dimensions": ["d_city"], "filters": {}, "unresolved": []}
    controller._salvage_time_axis("按送达月看各城市销售额占比", payload)
    controller._salvage_share("按送达月看各城市销售额占比", payload)
    assert payload["time_axis"] == "t.c03"
    assert payload["share"] is True

    slots = Slots.model_validate(payload)
    assert slots.time_axis == "t.c03"
    assert slots.share is True


def test_comparison_drops_inherited_dimensions(tmp_path):
    from app.core.controller import Controller
    from app.core.event_store import EventStore

    controller = Controller(
        EventStore(tmp_path / "sessions"), _sem(_sum_metric()), None, None, None,
    )
    slots = {
        "metric": "m",
        "dimensions": ["d_city"],
        "compare": {"type": "previous_period"},
        "limit_per_group": 1,
        "order_by": [{"field": "value", "direction": "desc"}],
        "notes": [],
    }
    controller._drop_implicit_dims_for_comparison("2025年5月比4月卖得多吗", slots)
    assert slots["dimensions"] == []
    assert slots["limit_per_group"] is None
    assert slots["order_by"] == []

    kept = {"metric": "m", "dimensions": ["d_city"], "compare": {"type": "previous_period"}}
    controller._drop_implicit_dims_for_comparison("各城市5月比4月卖得多吗", kept)
    assert kept["dimensions"] == ["d_city"]


def test_alternate_time_axis_uses_its_own_coverage():
    from app.core.state import Slots
    from app.domain.capability import assess_slots

    axes = [
        {"ref": "t.c02", "name_zh": "下单时间", "label": "purchase_at",
         "coverage": ["2024-01-01", "2025-12-31"]},
        {"ref": "t.c03", "name_zh": "送达时间", "label": "delivered_at",
         "coverage": ["2025-05-01", "2025-05-31"]},
    ]
    sem = _sem(_sum_metric(), axes)
    slots = Slots(
        metric="m",
        time=TimeRange(start=date(2025, 6, 1), end=date(2025, 7, 1)),
        time_axis="t.c03",
    )
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert any(item.kind == "time" for item in report.missing)
