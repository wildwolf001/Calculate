from __future__ import annotations

from app.core.state import Slots
from app.domain.planner import plan_from_ir, plan_from_slots
from app.domain.query.ir import QueryIR
from app.domain.query.metric import MetricSpec
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.parse_intent import LLMIntentParser


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
    spec = MetricSpec.from_input(
        {"aggregation": "AVG", "operand": "t1.c04", "fact": "t1", "label": "平均"},
        _sem(),
    )
    slots = Slots(
        metric=spec.id,
        metric_spec=spec,
        metric_source="inferred",
        dimensions=["d"],
        time={"start": "2025-01-01", "end": "2025-02-01"},
    )
    ir = QueryIR.from_slots(slots)
    assert ir.metric == spec.id
    assert ir.metric_spec.name == "平均"
    assert ir.metric_source == "inferred"
    lowered = ir.to_slots()
    assert lowered.metric_spec.name == "平均"
    assert lowered.metric_source == "inferred"


def test_single_dimension_per_group_becomes_overall_limit():
    sem = _sem()
    slots = Slots(metric="m", dimensions=["d"], limit=200, limit_per_group=1)
    plan = plan_from_ir(QueryIR.from_slots(slots), sem)
    assert plan.limit == 1
    assert plan.limit_per_group is None


def test_metric_spec_normalizes_time_via_and_round_trips_raw():
    sem = _sem()
    first = MetricSpec.from_input(
        {"aggregation": "AVG", "operand": "t1.c04", "fact": "t1", "label": "平均"},
        sem,
    )
    assert first.time_via == "t1.c02"
    second = MetricSpec.from_raw(first.to_raw())
    assert second.expr == "AVG(t1.c04)"
    assert second.time_via == "t1.c02"


def test_parser_normalized_metric_expr_reaches_planner(tmp_path):
    sem = _sem()
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    parser = LLMIntentParser(sem, client=None, dim_values_path=dim_values)
    raw_slots = parser._validate_slots({
        "metric_expr": {
            "aggregation": "AVG",
            "operand": "t1.c04",
            "fact": "t1",
            "label": "平均评分",
        }
    })
    slots = Slots.model_validate(raw_slots)
    assert isinstance(slots.metric_spec, MetricSpec)
    plan = plan_from_ir(QueryIR.from_slots(slots), sem)
    assert plan.metric_spec is not None
    assert plan.metric_spec["expr"] == "AVG(t1.c04)"
    assert plan.metric_spec["time_via"] == "t1.c02"


def test_plan_from_ir_matches_plan_from_slots():
    sem = _sem()
    slots = Slots(metric="m", dimensions=[], limit=10)
    by_slots = plan_from_slots(slots, sem)
    by_ir = plan_from_ir(QueryIR.from_slots(slots), sem)
    assert by_ir.metric == by_slots.metric
    assert by_ir.limit == by_slots.limit
    assert by_ir.plan_id == by_slots.plan_id
