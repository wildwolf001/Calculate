"""Public questions Q01–Q07 through the real pipeline."""
from __future__ import annotations

from datetime import date

import pytest

from app.domain.pipeline import run_query
from app.domain.plan import (
    CompareSpec, HavingSpec, NestedSpec, OrderSpec, QueryPlan, TimeRange,
)

FULL = TimeRange(start=date(2024, 1, 1), end=date(2025, 12, 26))
APR = TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1))
MAY = TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1))


def run(sem, plan):
    outcome = run_query(plan, sem)
    assert outcome.ok, outcome.error
    assert outcome.result is not None
    return outcome


def test_q01_delivered_orders(sem):
    out = run(sem, QueryPlan(metric="order_count", time=FULL, limit=1))
    assert out.result.rows[0][0] == 10714


def test_q02_gmv(sem):
    out = run(sem, QueryPlan(metric="gmv", time=FULL, limit=1))
    assert out.result.rows[0][0] == pytest.approx(4858920.84, abs=0.01)


def test_q03_mom_growth(sem):
    out = run(sem, QueryPlan(metric="gmv", time=APR, compare=CompareSpec(), limit=1))
    row = out.result.rows[0]
    current, baseline, delta, growth = row
    assert growth == pytest.approx(0.15, abs=0.0001)
    assert current - baseline == pytest.approx(delta, abs=0.01)
    assert out.compiled.meta["baseline_range"] == ["2025-03-01", "2025-04-01"]


def test_q04_category_rank(sem):
    out = run(sem, QueryPlan(
        metric="gmv", dimensions=["category"], time=APR,
        order_by=[OrderSpec(field="value", direction="desc")], limit=10,
    ))
    top = out.result.rows[0]
    assert top[0] == "数码家电"
    assert top[1] == pytest.approx(105245.50, abs=0.01)


def test_q05_delay_rate_by_seller(sem):
    out = run(sem, QueryPlan(
        metric="delay_rate", dimensions=["seller"], time=MAY,
        having=[HavingSpec(metric="order_count", op="gte", value=10)],
        ratio_grain="order",
        order_by=[OrderSpec(field="value", direction="desc")], limit=10,
    ))
    top = out.result.rows[0]
    assert top[0] == "广州美妆个护商家004"
    assert top[1] == 54 and top[2] == 59
    assert top[3] == pytest.approx(0.9153, abs=0.0001)
    assert out.facts["ratio_grain"] == "order"


def test_q06_score_by_lateness(sem):
    out = run(sem, QueryPlan(metric="avg_review_score", dimensions=["late"], time=FULL, limit=10))
    scores = {r[0]: r[1] for r in out.result.rows}
    assert scores["延迟"] == pytest.approx(2.00, abs=0.01)
    assert scores["准时"] == pytest.approx(4.02, abs=0.01)
    assert any(c.id == "COVERAGE" for c in out.checks)


def test_q07_repeat_customers(sem):
    out = run(sem, QueryPlan(
        metric="repeat_customer_count", time=FULL,
        nested=NestedSpec(min_orders=2), limit=1,
    ))
    assert out.result.rows[0][0] == 2378


def test_ratio_uses_order_grain_not_rows(sem):
    """Q05 evidence: row-level average (94.74%) must not appear."""
    out = run(sem, QueryPlan(
        metric="delay_rate", dimensions=["seller"], time=MAY,
        having=[HavingSpec(metric="order_count", op="gte", value=10)],
        ratio_grain="order",
        order_by=[OrderSpec(field="value", direction="desc")], limit=10,
    ))
    assert out.result.rows[0][3] != pytest.approx(0.9474, abs=0.0001)
