"""Regression tests for the 2026-11-11 governance fixes."""
from __future__ import annotations

from app.core.state import Slots
from app.datasets.index import build_index, semantic_fingerprint
from app.domain.capability import apply_degradations, assess_slots
from app.domain.semantic.graph import build_capabilities, same_name_dimension
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="fixture",
        tables={
            "orders": {
                "grain": ["order_id"], "time": "order_date",
                "columns": ["order_id", "state", "order_date", "amount"],
            },
            "items": {
                "grain": ["item_id"], "time": None,
                "columns": ["item_id", "order_id", "state"],
            },
        },
        joins={"items": {"orders": {"left": "order_id", "right": "order_id", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "total": Metric("total", {
                "name_zh": "总额", "type": "aggregate", "fact": "orders",
                "expr": "SUM(orders.amount)",
            }),
            "rate": Metric("rate", {
                "name_zh": "延迟率", "type": "ratio", "fact": "orders",
                "numerator": "COUNT(CASE WHEN orders.state = 'late' THEN 1 END)",
                "denominator": "COUNT(DISTINCT orders.order_id)",
                "scope": {},
            }),
        },
        dimensions={
            "d_state_orders": Dimension("d_state_orders", {
                "name_zh": "订单状态", "kind": "categorical", "column": "orders.state",
            }),
            "d_state_items": Dimension("d_state_items", {
                "name_zh": "订单状态", "kind": "categorical", "column": "items.state",
            }),
        },
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        capabilities={},
        raw={},
        time_axes=[],
    )


def test_same_name_dimension_swaps_to_safe_copy():
    sem = _sem()
    caps = build_capabilities(sem)
    assert "d_state_orders" in caps["total"].allowed_dimensions
    assert "d_state_items" not in caps["total"].allowed_dimensions

    assert same_name_dimension(sem, "total", "d_state_items") == "d_state_orders"

    slots = Slots(metric="total", dimensions=["d_state_items"])
    report = assess_slots(slots, sem)
    assert not report.refused
    replacements = [
        item for item in report.degradations
        if item.get("action") == "replace"
    ]
    assert replacements and replacements[0]["to"] == "d_state_orders"

    applied = apply_degradations(slots, report)
    assert applied.dimensions == ["d_state_orders"]


def test_index_strips_pii_dimension_values():
    sem = _sem()
    sem.dimensions["d_state_orders"].raw["column"] = "orders.c02"
    catalog = {
        "tables": {
            "orders": {
                "columns": [
                    {"name": "c02", "label": "email", "type": "varchar",
                     "pii": True, "samples": ["a@b.c"]},
                ],
            },
        },
    }
    dim_values = {"values": {"orders.c02": ["a@b.c", "x@y.z"]}}
    index = build_index(
        dataset_id="ds_fixture",
        data_version="sha256:test",
        semantic_version=1,
        sem=sem,
        catalog=catalog,
        dim_values=dim_values,
    )
    dim = index["dimensions"]["d_state_orders"]
    assert dim["pii"] is True
    assert dim["values"] == []
    columns = index["tables"]["orders"]["columns"]
    assert columns[0]["pii"] is True and columns[0]["samples"] == []


def test_semantic_fingerprint_tracks_edits():
    sem = _sem()
    before = semantic_fingerprint(sem)
    sem.metrics["total"].raw["synonyms"] = ["营收"]
    assert semantic_fingerprint(sem) != before


def _ratio_result(rows):
    from app.domain.executor import ResultSet

    return ResultSet(
        result_id="", columns=["seller", "numerator", "denominator", "value"],
        column_types=["VARCHAR", "BIGINT", "BIGINT", "DOUBLE"],
        rows=[list(row) for row in rows], row_count=len(rows),
        truncated=False, sql="", params=[], fingerprint="", duration_ms=0,
    )


def test_ratio_ranking_promotes_adequate_sample():
    from app.domain.plan import OrderSpec, QueryPlan
    from app.domain.presentation import stabilize_ratio_ranking

    plan = QueryPlan(
        metric="rate", dimensions=["d_state_orders"],
        order_by=[OrderSpec(field="value", direction="desc")], limit=5,
    )
    result = _ratio_result([
        ("A", 3, 3, 1.0),
        ("B", 54, 59, 0.915254),
        ("C", 3, 4, 0.75),
    ])
    policy = stabilize_ratio_ranking(plan, result, min_denominator=5)
    assert policy is not None
    assert result.rows[0][0] == "B"
    assert policy["promoted_denominator"] == 59
    assert policy["demoted_denominator"] == 3

    # A top row that already meets the threshold must not be touched.
    stable = _ratio_result([("B", 54, 59, 0.915254), ("C", 3, 4, 0.75)])
    assert stabilize_ratio_ranking(plan, stable, min_denominator=5) is None
    assert stable.rows[0][0] == "B"


def test_ratio_ranking_planner_fetches_extra_rows():
    from app.domain.planner import plan_from_ir
    from app.domain.query.ir import QueryIR

    ratio_plan = plan_from_ir(
        QueryIR(metric="rate", dimensions=("d_state_orders",), limit=1), _sem()
    )
    assert ratio_plan.limit == 5
    assert ratio_plan.order_by and ratio_plan.order_by[0].field == "value"

    aggregate_plan = plan_from_ir(
        QueryIR(metric="total", dimensions=("d_state_orders",), limit=1), _sem()
    )
    assert aggregate_plan.limit == 1

