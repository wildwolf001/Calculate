"""Coverage diagnostics expose missing periods instead of only observed rows."""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.domain.checks import run_coverage_checks
from app.domain.plan import QueryPlan, TimeRange
from app.domain.pipeline import execute_prepared, prepare
from app.domain.presentation import display_spec
from app.domain.semantic.loader import Metric, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="覆盖测试",
        tables={"orders": {"grain": ["order_id"], "time": "order_date"}},
        joins={},
        pre_aggregates={},
        metrics={
            "sales": Metric("sales", {
                "name_zh": "销售额",
                "type": "aggregate",
                "fact": "orders",
                "expr": "SUM(orders.amount)",
                "time_via": "orders.order_date",
                "additive": True,
                "supported_grain": ["none", "month"],
                "unit": "元",
                "selected": True,
            })
        },
        dimensions={},
        policies={},
        time_range=("2025-01-01", "2025-06-01"),
        binding={"tables": {"orders": {}}},
        capabilities={},
        raw={},
    )


def test_coverage_builds_calendar_spine_and_missing_facts(tmp_path):
    db = tmp_path / "coverage.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE orders(order_id INTEGER, order_date DATE, amount DOUBLE)")
    con.execute("INSERT INTO orders VALUES (1,'2025-02-03',10),(2,'2025-03-04',20),(3,'2025-05-05',30)")
    con.close()
    sem = _sem()
    plan = QueryPlan(
        kind="coverage",
        metric="sales",
        time=TimeRange(start="2025-01-01", end="2025-06-01"),
        limit=100,
    )
    compiled, issues = prepare(plan, sem)
    assert not [issue for issue in issues if issue.severity == "error"]
    result = execute_prepared(compiled, db_path=db)
    issues, facts = run_coverage_checks(plan, sem.metric("sales"), result)
    assert facts["expected_periods"] == [
        "2025-01", "2025-02", "2025-03", "2025-04", "2025-05",
    ]
    assert facts["missing_periods"] == ["2025-01", "2025-04"]
    assert facts["observed_periods"] == ["2025-02", "2025-03", "2025-05"]
    assert facts["coverage_ratio"] == 0.6
    gap = next(issue for issue in issues if issue.id == "TIME_GAP")
    assert gap.severity == "warn"
    assert "2025-01" in gap.detail and "2025-04" in gap.detail
    display = display_spec(plan, sem, result)
    assert [column["key"] for column in display["columns"]] == [
        "month", "record_count", "value", "is_missing",
    ]
