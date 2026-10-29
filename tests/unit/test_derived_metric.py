from __future__ import annotations

import duckdb
import pytest

from app.core.state import Slots
from app.domain.pipeline import execute_prepared, prepare
from app.domain.planner import plan_from_slots
from app.domain.semantic.loader import SemanticLayer


def _sem():
    return SemanticLayer(
        version=1,
        dataset="derived",
        tables={
            "t1": {
                "grain": ["c01"],
                "time": "c02",
                "columns": ["c01", "c02", "c03", "c04"],
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


def _warehouse(tmp_path):
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t1 (c01 VARCHAR, c02 DATE, c03 VARCHAR, c04 DOUBLE)")
    con.executemany(
        "INSERT INTO t1 VALUES (?, ?, ?, ?)",
        [
            ("o1", "2025-05-01", "延迟", 2.0),
            ("o2", "2025-05-02", "延迟", 3.0),
            ("o3", "2025-05-03", "准时", 5.0),
            ("o4", "2025-05-04", "准时", 4.0),
        ],
    )
    con.close()
    return path


def test_derived_avg_metric_plans_and_executes(tmp_path):
    sem = _sem()
    slots = Slots(
        metric_expr={
            "aggregation": "AVG",
            "operand": "t1.c04",
            "fact": "t1",
            "time_via": "t1.c02",
            "label": "平均评分",
            "supported_dimensions": ["none", "d_late"],
        },
        metric_source="inferred",
        time={"start": "2025-05-01", "end": "2025-06-01"},
    )
    plan = plan_from_slots(slots, sem)
    assert plan.metric_spec is not None
    assert plan.metric_spec["expr"] == "AVG(t1.c04)"
    assert plan.metric_source == "inferred"

    compiled, issues = prepare(plan, sem)
    assert not [issue for issue in issues if issue.severity == "error"]
    result = execute_prepared(compiled, db_path=_warehouse(tmp_path))
    assert result.rows[0][0] == pytest.approx(3.5)


def test_derived_count_distinct_metric(tmp_path):
    sem = _sem()
    slots = Slots(
        metric_expr={
            "aggregation": "COUNT_DISTINCT",
            "operand": "t1.c03",
            "fact": "t1",
            "label": "渠道数",
        },
        time={"start": "2025-05-01", "end": "2025-06-01"},
    )
    plan = plan_from_slots(slots, sem)
    compiled, _ = prepare(plan, sem)
    result = execute_prepared(compiled, db_path=_warehouse(tmp_path))
    assert result.rows[0][0] == 2
