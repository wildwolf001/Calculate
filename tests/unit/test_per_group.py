from __future__ import annotations

import duckdb

from app.domain.plan import OrderSpec, QueryPlan
from app.domain.pipeline import execute_prepared, prepare
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.domain.validator import validate_plan


def _warehouse(tmp_path):
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (c01 VARCHAR, c02 VARCHAR, c03 DOUBLE)")
    con.execute(
        "INSERT INTO t VALUES ('A','a1',100),('A','a2',50),"
        "('B','b1',80),('B','b2',70)"
    )
    con.close()
    return path


def _sem():
    return SemanticLayer(
        version=1, dataset="t",
        tables={"t": {"grain": ["c01"], "time": None, "columns": ["c01", "c02", "c03"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "销售额", "type": "aggregate", "fact": "t",
            "expr": "SUM(t.c03)", "supported_grain": ["none", "d_city", "d_product"],
        })},
        dimensions={
            "d_city": Dimension("d_city", {"id": "d_city", "name_zh": "城市", "column": "t.c01"}),
            "d_product": Dimension("d_product", {"id": "d_product", "name_zh": "商品", "column": "t.c02"}),
        },
        policies={}, time_range=("2020-01-01", "2030-01-01"),
        binding={"tables": {}}, raw={},
    )


def test_per_group_top_n_returns_one_row_per_group(tmp_path):
    plan = QueryPlan(
        metric="m", dimensions=["d_city", "d_product"],
        order_by=[OrderSpec(field="value", direction="desc")],
        limit=100, limit_per_group=1,
    )
    compiled, _ = prepare(plan, _sem())
    assert "QUALIFY" in compiled.sql
    result = execute_prepared(compiled, db_path=_warehouse(tmp_path))
    assert result.rows == [["A", "a1", 100.0], ["B", "b1", 80.0]]


def test_per_group_requires_two_dimensions():
    plan = QueryPlan(metric="m", dimensions=["d_city"], limit=10, limit_per_group=1)
    codes = [issue.code for issue in validate_plan(plan, _sem())]
    assert "BAD_PER_GROUP" in codes


def test_infer_limit_per_group_from_question():
    from app.core.controller import Controller
    from app.core.state import Slots

    slots = Slots(metric="m", dimensions=["d_city", "d_product"])
    Controller._infer_limit_per_group("每个城市卖的最好的商品是什么", slots)
    assert slots.limit_per_group == 1

    other = Slots(metric="m", dimensions=["d_city", "d_product"])
    Controller._infer_limit_per_group("每个城市的销售额分别是多少", other)
    assert other.limit_per_group is None
