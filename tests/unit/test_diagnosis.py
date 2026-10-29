from __future__ import annotations

from datetime import date

import duckdb

from app.domain.diagnosis import diagnose_empty
from app.domain.plan import FilterSpec, QueryPlan, TimeRange
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _warehouse(tmp_path):
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t_fact (c01 VARCHAR, c02 TIMESTAMP, c03 DOUBLE)")
    con.execute("INSERT INTO t_fact VALUES ('a','2025-05-10',3.0), ('b','2025-05-11',4.0)")
    con.execute("CREATE TABLE t_null (c01 VARCHAR, c02 TIMESTAMP, c03 DOUBLE)")
    con.execute("INSERT INTO t_null VALUES ('a','2025-05-10',NULL), ('b','2025-05-11',NULL)")
    con.execute("CREATE TABLE t_empty (c01 VARCHAR, c02 TIMESTAMP, c03 DOUBLE)")
    con.close()
    return path


def _sem():
    tables = {
        "t_fact": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03"]},
        "t_null": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03"]},
        "t_empty": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03"]},
    }
    metrics = {
        "m_fact": Metric("m_fact", {
            "name_zh": "金额", "type": "aggregate", "fact": "t_fact",
            "expr": "SUM(t_fact.c03)", "time_via": "t_fact.c02",
            "supported_grain": ["none", "d_city"],
        }),
        "m_null": Metric("m_null", {
            "name_zh": "空金额", "type": "aggregate", "fact": "t_null",
            "expr": "SUM(t_null.c03)", "time_via": "t_null.c02",
            "supported_grain": ["none"],
        }),
        "m_empty": Metric("m_empty", {
            "name_zh": "空表金额", "type": "aggregate", "fact": "t_empty",
            "expr": "SUM(t_empty.c03)", "time_via": "t_empty.c02",
            "supported_grain": ["none"],
        }),
    }
    dimensions = {
        "d_city": Dimension("d_city", {
            "id": "d_city", "name_zh": "城市",
            "column": "t_fact.c01", "filter_column": "t_fact.c01",
        }),
    }
    return SemanticLayer(
        version=1, dataset="t", tables=tables, joins={}, pre_aggregates={},
        metrics=metrics, dimensions=dimensions, policies={},
        time_range=("2024-01-01", "2025-12-31"), binding={"tables": {}}, raw={},
    )


def test_fact_empty(tmp_path):
    result = diagnose_empty(QueryPlan(metric="m_empty"), _sem(), _warehouse(tmp_path))
    assert result["code"] == "FACT_EMPTY"


def test_no_rows_in_window(tmp_path):
    plan = QueryPlan(
        metric="m_fact",
        time=TimeRange(start=date(2026, 1, 1), end=date(2026, 2, 1)),
    )
    result = diagnose_empty(plan, _sem(), _warehouse(tmp_path))
    assert result["code"] == "NO_ROWS_IN_WINDOW"
    assert result["facts"]["fact_rows"] == 2


def test_filter_excludes_all(tmp_path):
    plan = QueryPlan(
        metric="m_fact",
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
        filters={"d_city": FilterSpec(op="eq", value="nowhere")},
    )
    result = diagnose_empty(plan, _sem(), _warehouse(tmp_path))
    assert result["code"] == "FILTER_EXCLUDES_ALL"


def test_measure_all_null(tmp_path):
    plan = QueryPlan(
        metric="m_null",
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
    )
    result = diagnose_empty(plan, _sem(), _warehouse(tmp_path))
    assert result["code"] == "MEASURE_ALL_NULL"


def test_health_reports_rows_null_rate_and_coverage(tmp_path):
    from app.datasets.health import build_metric_health

    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (c01 VARCHAR, c02 TIMESTAMP, c03 DOUBLE)")
    con.execute("INSERT INTO t VALUES ('a','2025-05-01',1.0), ('b','2025-05-02',NULL)")
    con.close()

    sem = SemanticLayer(
        version=1, dataset="t",
        tables={"t": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "金额", "type": "aggregate", "fact": "t",
            "expr": "SUM(t.c03)", "time_via": "t.c02",
        })},
        dimensions={}, policies={},
        time_range=("2024-01-01", "2025-12-31"), binding={"tables": {}}, raw={},
    )
    health = build_metric_health(sem, ["m"], path)
    assert health["m"]["rows"] == 2
    assert health["m"]["measure_null_rate"] == 0.5
    assert health["m"]["time_coverage"] == ["2025-05-01", "2025-05-02"]
