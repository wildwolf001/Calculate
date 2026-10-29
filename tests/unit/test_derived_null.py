from __future__ import annotations

import duckdb

from app.domain.plan import FilterSpec, QueryPlan
from app.domain.pipeline import execute_prepared, prepare
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _setup(tmp_path):
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (c01 VARCHAR, c02 TIMESTAMP, c03 TIMESTAMP, c04 DOUBLE)")
    con.execute(
        "INSERT INTO t VALUES ('a','2025-01-01','2025-01-02',1.0),"
        "('b',NULL,'2025-01-02',2.0)"
    )
    con.close()
    sem = SemanticLayer(
        version=1, dataset="t",
        tables={"t": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03", "c04"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "金额", "type": "aggregate", "fact": "t",
            "expr": "SUM(t.c04)", "supported_grain": ["none", "d_delivery"],
        })},
        dimensions={"d_delivery": Dimension("d_delivery", {
            "id": "d_delivery", "name_zh": "是否延迟", "kind": "derived",
            "expr": "t.c02 > t.c03", "requires": "t",
            "labels": {"true": "延迟", "false": "准时"},
        })},
        policies={}, time_range=("2020-01-01", "2030-01-01"),
        binding={"tables": {}}, raw={},
    )
    return path, sem


def test_null_predicate_groups_as_null(tmp_path):
    path, sem = _setup(tmp_path)
    compiled, _ = prepare(
        QueryPlan(metric="m", dimensions=["d_delivery"]), sem,
    )
    rows = execute_prepared(compiled, db_path=path).rows
    assert [None, 2.0] in rows
    assert ["准时", 1.0] in rows


def test_ontime_filter_excludes_null_predicate(tmp_path):
    path, sem = _setup(tmp_path)
    plan = QueryPlan(
        metric="m",
        filters={"d_delivery": FilterSpec(op="eq", value="准时")},
    )
    compiled, _ = prepare(plan, sem)
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [[1.0]]
