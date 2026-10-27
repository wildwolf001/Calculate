"""Data quality problems should inform answers, not become pipeline crashes."""
from __future__ import annotations

from app.domain.checks import run_checks, run_member_list_checks
from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import Metric, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="测试",
        tables={"orders": {"grain": ["id"]}},
        joins={},
        pre_aggregates={},
        metrics={
            "orders": Metric("orders", {
                "name_zh": "订单数",
                "type": "aggregate",
                "fact": "orders",
                "expr": "COUNT(*)",
                "additive": True,
                "supported_grain": ["none"],
                "selected": True,
            })
        },
        dimensions={},
        policies={},
        time_range=("2025-01-01", "2025-02-01"),
        binding={"tables": {}},
        capabilities={},
        raw={},
    )


def _result(rows):
    return ResultSet(
        result_id="r1",
        columns=["value"],
        column_types=["BIGINT"],
        rows=rows,
        row_count=len(rows),
        truncated=False,
        sql="SELECT 1 LIMIT 1",
        params=[],
        fingerprint="fp",
        duration_ms=1,
        meta={},
    )


def test_quality_issue_is_attached_as_warning():
    issues, facts = run_checks(
        QueryPlan(metric="orders", limit=1),
        _sem().metric("orders"),
        _result([[3]]),
        _sem(),
        quality={"checks": [{
            "id": "empty_columns",
            "severity": "warn",
            "title": "存在全空列",
            "count": 2,
        }]},
    )
    issue = next(item for item in issues if item.id == "DATA_QUALITY")
    assert issue.severity == "warn"
    assert "可用记录" in issue.detail
    assert facts["data_quality"][0]["count"] == 2


def test_empty_result_is_answerable_with_missing_data_notice():
    issues, facts = run_checks(
        QueryPlan(metric="orders", limit=1),
        _sem().metric("orders"),
        _result([]),
        _sem(),
        quality={},
    )
    empty = next(item for item in issues if item.id == "EMPTY_RESULT")
    assert empty.severity == "warn"
    assert facts["empty_result"] is True


def test_member_list_gets_the_same_quality_notice():
    issues, _ = run_member_list_checks(
        QueryPlan(kind="member_list", list_dim=None, limit=5),
        _result([["A"], ["B"]]),
        quality={"checks": [{
            "id": "negative_values",
            "severity": "warn",
            "title": "存在负值",
            "count": 4,
        }]},
    )
    assert any(item.id == "DATA_QUALITY" for item in issues)
