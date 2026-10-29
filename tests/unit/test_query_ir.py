from __future__ import annotations

from app.core.state import Slots
from app.domain.planner import plan_from_ir, plan_from_slots
from app.domain.query.ir import QueryIR
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem():
    return SemanticLayer(
        version=1,
        dataset="ir",
        tables={"t1": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c04"]}},
        joins={},
        pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "记录数", "type": "aggregate", "fact": "t1",
            "expr": "COUNT(*)", "time_via": "t1.c02", "supported_grain": ["none"],
        })},
        dimensions={
            "d": Dimension("d", {"name_zh": "渠道", "kind": "categorical", "column": "t1.c04"}),
        },
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        raw={},
    )


def test_query_ir_round_trip_keeps_derived_metric():
    slots = Slots(
        metric="derived_x",
        metric_expr={"aggregation": "AVG", "operand": "t1.c04", "fact": "t1", "label": "平均"},
        metric_source="inferred",
        dimensions=["d"],
        time={"start": "2025-01-01", "end": "2025-02-01"},
    )
    ir = QueryIR.from_slots(slots)
    assert ir.metric == "derived_x"
    assert ir.metric_expr["label"] == "平均"
    assert ir.metric_source == "inferred"
    lowered = ir.to_slots()
    assert lowered.metric_expr["label"] == "平均"
    assert lowered.metric_source == "inferred"


def test_single_dimension_per_group_becomes_overall_limit():
    sem = _sem()
    slots = Slots(metric="m", dimensions=["d"], limit=200, limit_per_group=1)
    plan = plan_from_ir(QueryIR.from_slots(slots), sem)
    assert plan.limit == 1
    assert plan.limit_per_group is None


def test_plan_from_ir_matches_plan_from_slots():
    sem = _sem()
    slots = Slots(metric="m", dimensions=[], limit=10)
    by_slots = plan_from_slots(slots, sem)
    by_ir = plan_from_ir(QueryIR.from_slots(slots), sem)
    assert by_ir.metric == by_slots.metric
    assert by_ir.limit == by_slots.limit
    assert by_ir.plan_id == by_slots.plan_id
