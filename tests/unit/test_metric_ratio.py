from __future__ import annotations

import duckdb
import pytest

from app.domain.pipeline import PipelineError, execute_prepared, prepare
from app.domain.plan import CompareSpec, QueryPlan, TimeRange
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem(extra: dict | None = None) -> SemanticLayer:
    metrics = {
        "num": Metric("num", {
            "name_zh": "金额", "type": "aggregate", "fact": "t",
            "expr": "SUM(t.c04)",
        }),
        "den": Metric("den", {
            "name_zh": "订单数", "type": "aggregate", "fact": "t",
            "expr": "COUNT(DISTINCT t.c01)",
        }),
    }
    metrics.update(extra or {})
    return SemanticLayer(
        version=1, dataset="t",
        tables={
            "t": {"grain": ["c01"], "time": None, "columns": ["c01", "c02", "c04"]},
            "u": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
        },
        joins={}, pre_aggregates={},
        metrics=metrics,
        dimensions={"d_city": Dimension("d_city", {
            "id": "d_city", "name_zh": "城市", "column": "t.c01",
        })},
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def _warehouse(tmp_path):
    path = tmp_path / "w.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (c01 VARCHAR, c02 VARCHAR, c04 DOUBLE)")
    con.execute("INSERT INTO t VALUES ('A','x',100),('B','x',50)")
    con.close()
    return path


def test_metric_ratio_compiles_and_executes(tmp_path):
    plan = QueryPlan(
        metric="num", ratio_denominator="den", dimensions=["d_city"],
    )
    compiled, _ = prepare(plan, _sem())
    assert "NULLIF" in compiled.sql
    rows = execute_prepared(compiled, db_path=_warehouse(tmp_path)).rows
    assert rows[0] == ["A", 100.0]
    assert rows[1] == ["B", 50.0]


def test_metric_ratio_rejects_different_fact():
    extra = {"other": Metric("other", {
        "name_zh": "其他", "type": "aggregate", "fact": "u",
        "expr": "SUM(u.c02)",
    })}
    plan = QueryPlan(metric="num", ratio_denominator="other")
    with pytest.raises(PipelineError):
        prepare(plan, _sem(extra))


def test_capability_refuses_cross_fact_ratio():
    from app.core.state import Slots
    from app.domain.capability import assess_slots

    extra = {"other": Metric("other", {
        "name_zh": "其他", "type": "aggregate", "fact": "u",
        "expr": "SUM(u.c02)",
    })}
    sem = _sem(extra)
    slots = Slots(metric="num", ratio_numerator="num", ratio_denominator="other")
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "METRIC_RATIO_FACT_MISMATCH"


def _cross_sem() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="x",
        tables={
            "items": {"grain": ["item_id"], "time": None,
                      "columns": ["order_id", "item_id", "amount"]},
            "orders": {"grain": ["order_id"], "time": None,
                       "columns": ["order_id"]},
        },
        joins={"items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "金额", "type": "aggregate", "fact": "items",
                "expr": "SUM(items.amount)",
            }),
            "orders": Metric("orders", {
                "name_zh": "订单数", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(DISTINCT orders.order_id)",
            }),
        },
        dimensions={}, policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def test_cross_fact_ratio_uses_common_grain(tmp_path):
    path = tmp_path / "x.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES ('o1','i1',100),('o1','i2',50),('o2','i3',25)")
    con.execute("CREATE TABLE orders (order_id VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('o1'),('o2')")
    con.close()

    plan = QueryPlan(metric="sales", ratio_denominator="orders")
    compiled, _ = prepare(plan, _cross_sem())
    assert "num_grain" in compiled.sql
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [[87.5]]


def _cross_sem_grouped() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="x",
        tables={
            "items": {"grain": ["item_id"], "time": None,
                      "columns": ["order_id", "item_id", "amount"]},
            "orders": {"grain": ["order_id"], "time": None,
                       "columns": ["order_id", "customer_id"]},
            "customers": {"grain": ["customer_id"], "time": None,
                          "columns": ["customer_id", "city"]},
        },
        joins={
            "items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}},
            "orders": {"customers": {"left": "customer_id", "right": "customer_id", "card": "N:1"}},
        },
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "金额", "type": "aggregate", "fact": "items",
                "expr": "SUM(items.amount)",
            }),
            "orders": Metric("orders", {
                "name_zh": "订单数", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(DISTINCT orders.order_id)",
            }),
        },
        dimensions={"d_city": Dimension("d_city", {
            "id": "d_city", "name_zh": "城市", "column": "customers.city",
        })},
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def test_cross_fact_ratio_groups_and_filters(tmp_path):
    path = tmp_path / "g.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES ('o1','i1',100),('o1','i2',50),('o2','i3',25)")
    con.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('o1','c1'),('o2','c2')")
    con.execute("CREATE TABLE customers (customer_id VARCHAR, city VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('c1','A'),('c2','B')")
    con.close()

    plan = QueryPlan(
        metric="sales", ratio_denominator="orders", dimensions=["d_city"],
    )
    compiled, _ = prepare(plan, _cross_sem_grouped())
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [["A", 150.0], ["B", 25.0]]

    from app.domain.plan import FilterSpec
    filtered = QueryPlan(
        metric="sales", ratio_denominator="orders", dimensions=["d_city"],
        filters={"d_city": FilterSpec(op="eq", value="A")},
    )
    compiled, _ = prepare(filtered, _cross_sem_grouped())
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [["A", 150.0]]


def _cross_sem_time() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="x",
        tables={
            "items": {"grain": ["item_id"], "time": None,
                      "columns": ["order_id", "item_id", "amount"]},
            "orders": {"grain": ["order_id"], "time": "order_date",
                       "columns": ["order_id", "order_date"]},
        },
        joins={"items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "金额", "type": "aggregate", "fact": "items",
                "expr": "SUM(items.amount)", "time_via": "orders.order_date",
            }),
            "orders": Metric("orders", {
                "name_zh": "订单数", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(DISTINCT orders.order_id)", "time_via": "orders.order_date",
            }),
        },
        dimensions={"month": Dimension("month", {
            "id": "month", "name_zh": "月份", "kind": "time",
        })},
        policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def test_cross_fact_ratio_groups_by_month(tmp_path):
    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES ('o1','i1',100),('o1','i2',50),('o2','i3',25)")
    con.execute("CREATE TABLE orders (order_id VARCHAR, order_date DATE)")
    con.execute("INSERT INTO orders VALUES ('o1','2025-05-01'),('o2','2025-05-02')")
    con.close()

    plan = QueryPlan(metric="sales", ratio_denominator="orders", dimensions=["month"])
    compiled, _ = prepare(plan, _cross_sem_time())
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [["2025-05", 87.5]]


def test_cross_fact_ratio_share(tmp_path):
    path = tmp_path / "s.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES ('o1','i1',100),('o1','i2',50),('o2','i3',25)")
    con.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('o1','c1'),('o2','c2')")
    con.execute("CREATE TABLE customers (customer_id VARCHAR, city VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('c1','A'),('c2','B')")
    con.close()

    plan = QueryPlan(
        metric="sales", ratio_denominator="orders", dimensions=["d_city"],
        share=True,
    )
    compiled, _ = prepare(plan, _cross_sem_grouped())
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows[0][0] == "A"
    assert rows[0][2] == pytest.approx(150 / 175)
    assert rows[1][2] == pytest.approx(25 / 175)


def test_cross_fact_ratio_per_group(tmp_path):
    sem = SemanticLayer(
        version=1, dataset="x",
        tables={
            "items": {"grain": ["item_id"], "time": None,
                      "columns": ["order_id", "item_id", "amount"]},
            "orders": {"grain": ["order_id"], "time": None,
                       "columns": ["order_id", "customer_id", "channel"]},
            "customers": {"grain": ["customer_id"], "time": None,
                          "columns": ["customer_id", "city"]},
        },
        joins={
            "items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}},
            "orders": {"customers": {"left": "customer_id", "right": "customer_id", "card": "N:1"}},
        },
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "金额", "type": "aggregate", "fact": "items",
                "expr": "SUM(items.amount)",
            }),
            "orders": Metric("orders", {
                "name_zh": "订单数", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(DISTINCT orders.order_id)",
            }),
        },
        dimensions={
            "d_city": Dimension("d_city", {"id": "d_city", "name_zh": "城市", "column": "customers.city"}),
            "d_channel": Dimension("d_channel", {"id": "d_channel", "name_zh": "渠道", "column": "orders.channel"}),
        },
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )
    path = tmp_path / "p.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES ('o1','i1',150),('o2','i2',25),('o3','i3',10)")
    con.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, channel VARCHAR)")
    con.execute("INSERT INTO orders VALUES ('o1','c1','ch1'),('o2','c2','ch1'),('o3','c1','ch2')")
    con.execute("CREATE TABLE customers (customer_id VARCHAR, city VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('c1','A'),('c2','B')")
    con.close()

    plan = QueryPlan(
        metric="sales", ratio_denominator="orders",
        dimensions=["d_city", "d_channel"], limit_per_group=1,
    )
    compiled, _ = prepare(plan, sem)
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [["A", "ch1", 150.0], ["B", "ch1", 25.0]]


def test_cross_fact_ratio_compares_periods(tmp_path):
    path = tmp_path / "c.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES "
                "('o1','i1',100),('o1','i2',50),('o2','i3',25),"
                "('o3','i4',10),('o4','i5',20)")
    con.execute("CREATE TABLE orders (order_id VARCHAR, order_date DATE)")
    con.execute("INSERT INTO orders VALUES "
                "('o1','2025-05-01'),('o2','2025-05-02'),"
                "('o3','2025-04-01'),('o4','2025-04-02')")
    con.close()

    plan = QueryPlan(
        metric="sales", ratio_denominator="orders",
        time=TimeRange(start=__import__("datetime").date(2025, 5, 1),
                       end=__import__("datetime").date(2025, 6, 1)),
        compare=CompareSpec(),
    )
    compiled, _ = prepare(plan, _cross_sem_time())
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [[87.5, 15.0, 72.5, pytest.approx(72.5 / 15)]]


def test_cross_fact_ratio_compares_by_dimension(tmp_path):
    sem = SemanticLayer(
        version=1, dataset="x",
        tables={
            "items": {"grain": ["item_id"], "time": None,
                      "columns": ["order_id", "item_id", "amount"]},
            "orders": {"grain": ["order_id"], "time": "order_date",
                       "columns": ["order_id", "customer_id", "order_date"]},
            "customers": {"grain": ["customer_id"], "time": None,
                          "columns": ["customer_id", "city"]},
        },
        joins={
            "items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}},
            "orders": {"customers": {"left": "customer_id", "right": "customer_id", "card": "N:1"}},
        },
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "金额", "type": "aggregate", "fact": "items",
                "expr": "SUM(items.amount)", "time_via": "orders.order_date",
            }),
            "orders": Metric("orders", {
                "name_zh": "订单数", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(DISTINCT orders.order_id)", "time_via": "orders.order_date",
            }),
        },
        dimensions={"d_city": Dimension("d_city", {
            "id": "d_city", "name_zh": "城市", "column": "customers.city",
        })},
        policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )
    path = tmp_path / "cd.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE items (order_id VARCHAR, item_id VARCHAR, amount DOUBLE)")
    con.execute("INSERT INTO items VALUES "
                "('o1','i1',150),('o2','i2',25),('o3','i3',10),('o4','i4',20)")
    con.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, order_date DATE)")
    con.execute("INSERT INTO orders VALUES "
                "('o1','c1','2025-05-01'),('o2','c2','2025-05-02'),"
                "('o3','c1','2025-04-01'),('o4','c2','2025-04-02')")
    con.execute("CREATE TABLE customers (customer_id VARCHAR, city VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('c1','A'),('c2','B')")
    con.close()

    plan = QueryPlan(
        metric="sales", ratio_denominator="orders", dimensions=["d_city"],
        time=TimeRange(start=__import__("datetime").date(2025, 5, 1),
                       end=__import__("datetime").date(2025, 6, 1)),
        compare=CompareSpec(),
    )
    compiled, _ = prepare(plan, sem)
    rows = execute_prepared(compiled, db_path=path).rows
    assert rows == [["A", 150.0, 10.0, 140.0, 14.0], ["B", 25.0, 20.0, 5.0, 0.25]]
