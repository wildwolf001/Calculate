"""Deterministic key facts: no ungrounded numbers, correct scope handling."""
from __future__ import annotations

from datetime import date

from app.domain.insights import build_insights
from app.domain.plan import ComparisonSpec, QueryPlan, TimeRange


def test_comparison_by_dimension_names_both_ends(sem):
    plan = QueryPlan(
        metric="gmv", dimensions=["category"],
        comparison=ComparisonSpec(
            current=TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1)),
            baseline=TimeRange(start=date(2025, 3, 1), end=date(2025, 4, 1)),
        ),
        limit=10,
    )
    cols = ["category", "current", "baseline", "delta", "growth_rate"]
    rows = [["数码家电", 105.0, 40.0, 65.0, 1.625],
            ["家居生活", 30.0, 50.0, -20.0, -0.4]]
    out = build_insights(plan, cols, rows, 2, False, [], sem)
    assert any("增量最大" in x and "数码家电" in x for x in out)
    assert any("拖累最大" in x and "家居生活" in x for x in out)
    assert any("增量合计" in x for x in out)


def test_ranking_reports_top1_share(sem):
    plan = QueryPlan(metric="gmv", dimensions=["category"], limit=10)
    out = build_insights(plan, ["category", "value"],
                         [["数码家电", 80.0], ["家居生活", 20.0]], 2, False, [], sem)
    assert any("Top1 数码家电" in x and "80.0%" in x for x in out)


def test_ratio_value_uses_counts_not_percent_units(sem):
    plan = QueryPlan(metric="delay_rate")
    out = build_insights(plan, ["numerator", "denominator", "value", "order_count"],
                         [[102, 409, 0.249389, 409]], 1, False, [], sem)
    assert out and "102 / 409" in out[0]
    assert "24.94%" in out[0]
    assert "10200" not in out[0]


def test_trend_reports_endpoints_and_extremes(sem):
    plan = QueryPlan(metric="gmv", dimensions=["month"], limit=100)
    rows = [["2025-03", 100.0], ["2025-04", 150.0], ["2025-05", 120.0]]
    out = build_insights(plan, ["month", "value"], rows, 3, False, [], sem)
    assert any("2025-03" in x and "2025-05" in x for x in out)
    assert any("最高 2025-04" in x for x in out)
