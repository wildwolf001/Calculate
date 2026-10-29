from __future__ import annotations

from datetime import date

from app.domain.plan import CompareSpec, HavingSpec, QueryPlan, TimeRange
from app.domain.recovery import candidates_for_failure, classify_error
from app.domain.semantic.loader import SemanticLayer


def _sem():
    return SemanticLayer(
        version=1, dataset="t", tables={}, joins={}, pre_aggregates={},
        metrics={}, dimensions={}, policies={},
        time_range=("2024-01-01", "2025-12-31"), binding={"tables": {}}, raw={},
    )


def _plan():
    return QueryPlan(
        metric="m",
        dimensions=["d1", "d2"],
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
        having=[HavingSpec(metric="order_count", op="gte", value=10)],
        compare=CompareSpec(),
    )


def test_candidates_are_least_destructive_first():
    plan = _plan()
    candidates = candidates_for_failure(plan, _sem())
    actions = [candidate.action for candidate in candidates]

    assert actions[0] == "drop_having"
    assert actions[1] == "drop_comparison"
    assert "drop_dimension" in actions
    assert actions[-1] == "widen_time"
    assert plan.dimensions == ["d1", "d2"]
    assert plan.having and plan.compare


def test_drop_dimension_removes_one_dimension_at_a_time():
    candidates = candidates_for_failure(_plan(), _sem())
    dim_candidates = [c for c in candidates if c.action == "drop_dimension"]
    assert [c.plan.dimensions for c in dim_candidates] == [["d1"], ["d2"]]


def test_repair_marks_degraded_reason():
    candidates = candidates_for_failure(_plan(), _sem())
    assert all(candidate.plan.degraded_reason for candidate in candidates)


def test_classify_error_codes():
    assert classify_error(Exception("Binder Error: Referenced column x"))[0] == "UNSUPPORTED_PLAN"
    assert classify_error(Exception("Conversion Error: could not convert"))[0] == "TYPE_MISMATCH"
    assert classify_error(TimeoutError("boom"))[0] == "TIMEOUT"
    assert classify_error(RuntimeError("x"))[0] == "EXECUTION_FAILED"
