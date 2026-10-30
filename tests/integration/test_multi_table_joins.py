from __future__ import annotations

import asyncio
from pathlib import Path

import duckdb
import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.domain.capability import assess_slots
from app.core.interfaces import ParseResult
from app.domain.pipeline import PipelineError, execute_prepared, prepare
from app.domain.resolver import RuleGrounder
from app.domain.planner import plan_from_ir
from app.domain.query.ir import QueryIR
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.compose_answer import TemplateComposer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="multi_table",
        tables={
            "customers": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
            "orders": {"grain": ["c01"], "time": "c04", "columns": ["c01", "c02", "c03", "c04", "c05"]},
            "products": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
            "categories": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
            "order_items": {"grain": ["c01", "c02"], "time": None, "columns": ["c01", "c02", "c03", "c04", "c05"]},
            "payments": {"grain": ["c01", "c02"], "time": None, "columns": ["c01", "c02"]},
        },
        joins={
            "order_items": {
                "orders": {"left": "c01", "right": "c01", "card": "N:1"},
                "products": {"left": "c03", "right": "c01", "card": "N:1"},
            },
            "products": {"categories": {"left": "c02", "right": "c01", "card": "N:1"}},
            "orders": {"customers": {"left": "c02", "right": "c01", "card": "N:1"}},
            "payments": {"orders": {"left": "c01", "right": "c01", "card": "N:1"}},
        },
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "销售额", "type": "aggregate", "fact": "order_items",
                "expr": "SUM(order_items.c05)",
                "scope": {"orders.c05": "delivered"}, "time_via": "orders.c04",
            }),
            "order_count": Metric("order_count", {
                "name_zh": "订单数", "type": "aggregate", "fact": "order_items",
                "expr": "COUNT(DISTINCT order_items.c01)",
                "scope": {"orders.c05": "delivered"}, "time_via": "orders.c04",
            }),
            "payment_total": Metric("payment_total", {
                "name_zh": "支付金额", "type": "aggregate", "fact": "payments",
                "expr": "SUM(payments.c02)",
                "scope": {"orders.c05": "delivered"}, "time_via": "orders.c04",
            }),
        },
        dimensions={
            "d_category": Dimension("d_category", {
                "id": "d_category", "name_zh": "品类", "column": "categories.c02",
            }),
            "d_region": Dimension("d_region", {
                "id": "d_region", "name_zh": "地区", "column": "customers.c02",
            }),
            "d_status": Dimension("d_status", {
                "id": "d_status", "name_zh": "状态", "column": "orders.c05",
            }),
        },
        policies={},
        time_range=("2024-01-01", "2024-12-31"),
        binding={"tables": {}},
        raw={},
    )


def _sem_with_order_amount() -> SemanticLayer:
    sem = _sem()
    sem.metrics["order_amount"] = Metric("order_amount", {
        "name_zh": "订单金额", "type": "aggregate", "fact": "orders",
        "expr": "SUM(orders.c03)", "time_via": "orders.c04",
    })
    sem.dimensions["d_item"] = Dimension("d_item", {
        "id": "d_item", "name_zh": "明细行", "column": "order_items.c02",
    })
    return sem


def _sem_bound() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="bound_join",
        tables={
            "orders": {"grain": ["c01"], "time": "c04", "columns": ["c01", "c02", "c03", "c04", "c05"]},
            "customers": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
        },
        joins={"orders": {"customers": {"left": "c02", "right": "c01", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "销售额", "type": "aggregate", "fact": "orders",
                "expr": "SUM(orders.c03)", "scope": {"orders.c05": "delivered"},
                "time_via": "orders.c04",
            }),
            "big_sales": Metric("big_sales", {
                "name_zh": "大额销售额", "type": "aggregate", "fact": "orders",
                "expr": "SUM(CASE WHEN orders.c03 > 50 THEN orders.c03 ELSE 0 END)",
                "scope": {"orders.c05": "delivered"}, "time_via": "orders.c04",
            }),
        },
        dimensions={
            "d_region": Dimension("d_region", {
                "id": "d_region", "name_zh": "地区", "column": "customers.c02",
            }),
            "d_big": Dimension("d_big", {
                "id": "d_big", "name_zh": "是否大额", "kind": "derived",
                "requires": "orders", "expr": "orders.c03 > 50",
                "labels": {"true": "大额", "false": "小额"},
            }),
        },
        policies={},
        time_range=("2024-01-01", "2024-12-31"),
        binding={"tables": {
            "orders": {
                "c01": "oid", "c02": "cust_id", "c03": "amt",
                "c04": "dt", "c05": "status",
            },
            "customers": {"c01": "cid", "c02": "region"},
        }},
        raw={},
    )


def test_join_columns_go_through_binding(tmp_path):
    path = tmp_path / "bound.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE orders (oid VARCHAR, cust_id VARCHAR, amt DOUBLE, dt DATE, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('O1','C1',80,'2024-01-01','delivered')")
    con.execute("CREATE TABLE customers (cid VARCHAR, region VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('C1','华东')")
    con.close()
    sem = _sem_bound()
    plan = plan_from_ir(QueryIR(metric="sales", dimensions=("d_region",)), sem)
    compiled, issues = prepare(plan, sem)
    assert not [i for i in issues if i.severity == "error"]
    result = execute_prepared(compiled, db_path=path)
    assert result.rows == [["华东", 80.0]]

    big = plan_from_ir(QueryIR(metric="big_sales", dimensions=("d_big",)), sem)
    big_compiled, issues = prepare(big, sem)
    assert not [i for i in issues if i.severity == "error"]
    big_result = execute_prepared(big_compiled, db_path=path)
    assert big_result.rows == [["大额", 80.0]]


def _warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "multi.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE customers (c01 VARCHAR, c02 VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('C1','华东'),('C2','华南')")
    con.execute("CREATE TABLE orders (c01 VARCHAR, c02 VARCHAR, c03 DOUBLE, c04 DATE, c05 VARCHAR)")
    con.execute(
        "INSERT INTO orders VALUES "
        "('O1','C1',100,'2024-01-10','delivered'),"
        "('O2','C1',200,'2024-02-10','delivered'),"
        "('O3','C2',300,'2024-03-10','cancelled')"
    )
    con.execute("CREATE TABLE products (c01 VARCHAR, c02 VARCHAR)")
    con.execute("INSERT INTO products VALUES ('P1','CAT_A'),('P2','CAT_B')")
    con.execute("CREATE TABLE categories (c01 VARCHAR, c02 VARCHAR)")
    con.execute("INSERT INTO categories VALUES ('CAT_A','数码'),('CAT_B','家居')")
    con.execute(
        "CREATE TABLE order_items "
        "(c01 VARCHAR, c02 VARCHAR, c03 VARCHAR, c04 INTEGER, c05 DOUBLE)"
    )
    con.execute(
        "INSERT INTO order_items VALUES "
        "('O1','I1','P1',1,10),('O1','I2','P2',1,20),"
        "('O2','I3','P1',1,50),('O3','I4','P2',1,99)"
    )
    con.execute("CREATE TABLE payments (c01 VARCHAR, c02 DOUBLE)")
    con.execute("INSERT INTO payments VALUES ('O1',40),('O1',60),('O2',200),('O3',300)")
    con.close()
    return path


def _run(tmp_path: Path, metric: str, dimensions=(), time=None, ratio=None):
    plan = plan_from_ir(
        QueryIR(
            metric=metric,
            ratio_numerator=ratio[0] if ratio else None,
            ratio_denominator=ratio[1] if ratio else None,
            dimensions=tuple(dimensions),
            time=time,
        ),
        _sem(),
    )
    compiled, issues = prepare(plan, _sem())
    assert not [i for i in issues if i.severity == "error"]
    result = execute_prepared(compiled, db_path=_warehouse(tmp_path))
    return compiled, result


class _CategoryParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=1.0,
            slots={"metric": "sales", "dimensions": ["d_category"]},
        )


def test_controller_turn_runs_multi_table_join(tmp_path):
    sem = _sem()
    warehouse = _warehouse(tmp_path)
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    ctrl = Controller(
        EventStore(tmp_path / "sessions"), sem, _CategoryParser(),
        RuleGrounder(sem, dim_values), TemplateComposer(),
    )
    ctrl.warehouse = warehouse

    async def consume():
        return [event async for event in ctrl.run_turn("s1", "按品类看销售额")]

    events = asyncio.run(consume())
    assert "sql.executed" in [event.type for event in events]
    state = EventStore(tmp_path / "sessions").replay("s1")
    artifact = state.artifacts[state.last_result_id]
    assert artifact["rows"] == [["家居", 20.0], ["数码", 60.0]]


def test_capability_accepts_ir_filter_dict():
    report = assess_slots(
        QueryIR(
            metric="sales",
            filters={"d_status": {"op": "eq", "value": "delivered"}},
        ),
        _sem(),
    )
    assert report.status in {"answerable", "degradable"}


def test_multi_hop_join_sales_by_category(tmp_path):
    compiled, result = _run(tmp_path, "sales", ["d_category"])
    assert 'JOIN "categories"' in compiled.sql
    assert result.rows == [["家居", 20.0], ["数码", 60.0]]


def test_join_to_parent_dimension_does_not_duplicate(tmp_path):
    compiled, result = _run(tmp_path, "sales", ["d_region"])
    assert '"orders"."c02"' in compiled.sql
    assert result.rows == [["华东", 80.0]]


def test_count_distinct_survives_item_fanout(tmp_path):
    _, result = _run(tmp_path, "order_count", ["d_category"])
    assert result.rows == [["家居", 1], ["数码", 2]]


def test_payment_sum_not_multiplied_by_order_items(tmp_path):
    _, result = _run(tmp_path, "payment_total")
    assert result.rows == [[300.0]]


def test_payment_sum_by_region(tmp_path):
    _, result = _run(tmp_path, "payment_total", ["d_region"])
    assert result.rows == [["华东", 300.0]]


def test_time_filter_on_joined_orders(tmp_path):
    _, result = _run(
        tmp_path, "sales", ["d_category"],
        time={"start": "2024-01-01", "end": "2024-03-01"},
    )
    assert result.rows == [["家居", 20.0], ["数码", 60.0]]


def test_two_dimensions_join_all_paths(tmp_path):
    compiled, result = _run(tmp_path, "sales", ["d_region", "d_category"])
    assert '"customers"."c02"' in compiled.sql
    assert '"categories"."c02"' in compiled.sql
    assert result.rows == [
        ["华东", "家居", 20.0],
        ["华东", "数码", 60.0],
    ]


def test_cross_fact_ratio_with_time_filter(tmp_path):
    _, result = _run(
        tmp_path, "sales_over_payment", ["d_region"],
        time={"start": "2024-01-01", "end": "2024-03-01"},
        ratio=("sales", "payment_total"),
    )
    assert result.rows == [["华东", pytest.approx(80.0 / 300.0, abs=1e-5)]]


def test_reverse_fanout_is_refused_instead_of_wrong_sql(tmp_path):
    from app.domain.query.ir import QueryIR
    from app.domain.planner import plan_from_ir

    plan = plan_from_ir(
        QueryIR(metric="order_amount", dimensions=("d_item",)),
        _sem_with_order_amount(),
    )
    with pytest.raises(PipelineError):
        prepare(plan, _sem_with_order_amount())


def test_cross_fact_ratio_uses_common_grain(tmp_path):
    _, result = _run(tmp_path, "sales_over_payment", ["d_region"],
                     ratio=("sales", "payment_total"))
    assert result.rows == [["华东", pytest.approx(80.0 / 300.0, abs=1e-5)]]
