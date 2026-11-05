from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb

from app.domain.pipeline import run_query
from app.domain.plan import QueryPlan, TimeRange
from app.domain.semantic.loader import Metric, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="quality",
        tables={"t": {"grain": ["c01"], "time": "c01", "columns": ["c01", "c02"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "销售额", "type": "aggregate", "fact": "t",
            "expr": "SUM(t.c02)", "time_via": "t.c01",
        })},
        dimensions={}, policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def test_quality_scan_reports_missing_month_and_negative(tmp_path):
    path = tmp_path / "q.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (c01 DATE, c02 DOUBLE)")
    con.execute(
        "INSERT INTO t VALUES "
        "('2025-01-10', 100), ('2025-01-11', -50), "
        "('2025-03-15', 80)"
    )
    con.close()
    outcome = run_query(
        QueryPlan(
            metric="m",
            time=TimeRange(start=date(2025, 1, 1), end=date(2025, 4, 1)),
        ),
        _sem(),
        db_path=path,
    )
    assert outcome.ok, outcome.error
    codes = {issue.id for issue in outcome.checks}
    assert "TIME_GAP" in codes
    assert "PARTIAL_PERIOD" in codes
    assert "NEGATIVE_VALUES" in codes
    facts = outcome.facts.get("quality_scan") or []
    neg = next(item for item in facts if item["code"] == "NEGATIVE_VALUES")
    assert neg["evidence"]["negative_rows"] == 1
    assert neg["evidence"]["negative_sum"] == -50.0
