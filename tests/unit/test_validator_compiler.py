from __future__ import annotations

from datetime import date

import pytest

from app.domain.compiler import compile_plan
from app.domain.plan import FilterSpec, QueryPlan, TimeRange
from app.domain.validator import validate_plan, validate_sql
from app.domain.semantic.loader import SemanticError

T = TimeRange(start=date(2025, 1, 1), end=date(2025, 2, 1))


def test_unknown_metric(sem):
    issues = validate_plan(QueryPlan(metric="does_not_exist", time=T), sem)
    assert any(i.code == "UNKNOWN_METRIC" and i.severity == "error" for i in issues)


def test_unsupported_grain(sem):
    issues = validate_plan(
        QueryPlan(metric="avg_review_score", dimensions=["category"], time=T), sem
    )
    assert any(i.code == "UNSUPPORTED_GRAIN" for i in issues)


def test_bad_limit(sem):
    issues = validate_plan(QueryPlan(metric="gmv", time=T, limit=999999), sem)
    assert any(i.code == "BAD_LIMIT" for i in issues)


def test_filter_values_are_bound_not_interpolated(sem):
    malicious = "x' OR 1=1 --"
    plan = QueryPlan(metric="gmv", time=T, filters={"category": FilterSpec(op="eq", value=malicious)})
    compiled = compile_plan(plan, sem)
    assert malicious not in compiled.sql
    assert malicious in [str(p) for p in compiled.params]


def test_compiled_sql_is_whitelisted_single_select(sem):
    plan = QueryPlan(metric="gmv", dimensions=["category"], time=T)
    compiled = compile_plan(plan, sem)
    issues = validate_sql(compiled.sql, compiled, plan, sem)
    assert [i for i in issues if i.severity == "error"] == []


def test_ratio_compiled_at_order_grain(sem):
    plan = QueryPlan(metric="delay_rate", dimensions=["seller"], time=T)
    compiled = compile_plan(plan, sem)
    assert "WITH grain AS" in compiled.sql
    assert "orders.order_id" in compiled.sql.replace('"', "")
    assert "GROUP BY" in compiled.sql


def test_payment_metric_preaggregates(sem):
    plan = QueryPlan(metric="paid_amount", time=T)
    compiled = compile_plan(plan, sem)
    assert "GROUP BY" in compiled.sql
    assert "order_payments" in compiled.sql


def test_compare_baseline_is_previous_calendar_month(sem):
    plan = QueryPlan(metric="gmv", time=T, compare=__import__("app.domain.plan", fromlist=["CompareSpec"]).CompareSpec())
    compiled = compile_plan(plan, sem)
    assert compiled.meta["baseline_range"] == ["2024-12-01", "2025-01-01"]


def test_missing_semantic_file_raises():
    with pytest.raises(SemanticError):
        from app.domain.semantic.loader import load_semantic
        load_semantic(semantic_path=__import__("pathlib").Path("nope.yaml"))
