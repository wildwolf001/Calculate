from __future__ import annotations

from pathlib import Path

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.state import Slots
from app.domain.capability import assess_slots
from app.domain.plan import FilterSpec
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.parse_intent import LLMIntentParser


def _sem(metrics, dimensions):
    return SemanticLayer(
        version=1,
        dataset="test",
        tables={
            "t1": {
                "grain": ["c01"],
                "time": "c02",
                "columns": ["c01", "c02", "c03", "c04", "c05"],
            }
        },
        joins={},
        pre_aggregates={},
        metrics={m.id: m for m in metrics},
        dimensions={d.id: d for d in dimensions},
        policies={},
        time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}},
        raw={},
    )


def _delay_dim():
    return Dimension("d_delay", {
        "id": "d_delay",
        "name_zh": "是否延迟",
        "kind": "derived",
        "expr": "t1.c03 > t1.c04",
        "labels": {"true": "延迟", "false": "准时"},
        "requires": "t1",
    })


def _ratio_metric():
    return Metric("m_delay_rate", {
        "name_zh": "延迟率",
        "type": "ratio",
        "fact": "t1",
        "numerator": "COUNT(CASE WHEN t1.c03 > t1.c04 THEN 1 END)",
        "denominator": "COUNT(DISTINCT t1.c01)",
        "grain_numerator": "MAX(CASE WHEN t1.c03 > t1.c04 THEN 1 ELSE 0 END)",
        "grain_denominator": "1",
        "time_via": "t1.c02",
        "supported_grain": ["none", "d_delay"],
    })


def _avg_metric():
    return Metric("m_avg_score", {
        "name_zh": "平均评分",
        "type": "aggregate",
        "fact": "t1",
        "expr": "AVG(t1.c05)",
        "time_via": "t1.c02",
        "supported_grain": ["none", "d_delay"],
    })


def _late_count_metric():
    return Metric("m_late_orders", {
        "name_zh": "延迟订单",
        "type": "aggregate",
        "fact": "t1",
        "expr": "COUNT(DISTINCT CASE WHEN t1.c03 > t1.c04 THEN t1.c01 END)",
        "time_via": "t1.c02",
        "supported_grain": ["none", "d_delay"],
    })


def _controller(tmp_path, metrics):
    sem = _sem(metrics, [_delay_dim()])
    return Controller(EventStore(tmp_path / "sessions"), sem, None, None, None), sem


def test_slot_allowlist_covers_prompt_schema():
    allowed = LLMIntentParser.ALLOWED_SLOT_KEYS
    for key in (
        "ratio_numerator",
        "ratio_denominator",
        "time_axis",
        "share",
        "limit_per_group",
    ):
        assert key in allowed

    prompt = (
        Path(__file__).resolve().parents[2]
        / "app" / "llm" / "prompts" / "parse_intent_system.txt"
    ).read_text(encoding="utf-8")
    for key in (
        "ratio_numerator",
        "ratio_denominator",
        "time_axis",
        "share",
        "limit_per_group",
    ):
        assert key in prompt


def test_subquestion_bad_slots_stay_query_with_error(tmp_path):
    controller, sem = _controller(tmp_path, [_avg_metric()])
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    parser = LLMIntentParser(sem, client=None, dim_values_path=dim_values)
    entry = {
        "question": "哪个商户延迟率最高？",
        "intent_type": "query",
        "context": {"action": "new"},
        "slots": {"metric": "m_avg_score", "bogus": 1},
    }
    out = parser._validate_subquestions([entry])[0]
    assert out["intent_type"] == "query"
    assert out["slots"] == {}
    assert "未知槽位字段" in out["error"]


def test_parser_accepts_derived_metric_expression(tmp_path):
    controller, sem = _controller(tmp_path, [_avg_metric()])
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    parser = LLMIntentParser(sem, client=None, dim_values_path=dim_values)
    slots = parser._validate_slots({
        "metric_expr": {
            "aggregation": "AVG",
            "operand": "t1.c05",
            "fact": "t1",
            "label": "平均评分",
        }
    })
    assert slots["metric_expr"]["expr"] == "AVG(t1.c05)"
    assert slots["metric_source"] == "inferred"
    assert slots["metric"].startswith("derived_")

    from app.llm.parse_intent import IntentParseError

    with pytest.raises(IntentParseError):
        parser._validate_slots({
            "metric_expr": {
                "aggregation": "AVG",
                "operand": "t1.missing",
                "fact": "t1",
            }
        })


def test_capability_accepts_derived_metric_expression(tmp_path):
    controller, sem = _controller(tmp_path, [_avg_metric()])
    slots = Slots(metric_expr={
        "aggregation": "AVG",
        "operand": "t1.c05",
        "fact": "t1",
        "label": "平均评分",
    })
    report = assess_slots(slots, sem)
    assert report.status == "answerable"


def test_ratio_metric_filtered_by_own_predicate_is_refused(tmp_path):
    controller, sem = _controller(tmp_path, [_ratio_metric()])
    slots = Slots(
        metric="m_delay_rate",
        filters={"d_delay": FilterSpec(op="eq", value="延迟")},
    )
    report = assess_slots(slots, sem)
    assert report.refused is True
    assert report.refusal_payload()["code"] == "RATIO_SELF_FILTER"


def test_ratio_metric_is_not_filtered_by_its_own_predicate(tmp_path):
    controller, _ = _controller(tmp_path, [_ratio_metric()])
    slots = {"metric": "m_delay_rate", "filters": {}}
    controller._salvage_derived_filter("延迟率最高的是谁", slots)
    assert slots["filters"] == {}


def test_metric_expr_containing_predicate_is_not_filtered(tmp_path):
    controller, _ = _controller(tmp_path, [_late_count_metric()])
    slots = {"metric": "m_late_orders", "filters": {}}
    controller._salvage_derived_filter("延迟订单数最多的是谁", slots)
    assert slots["filters"] == {}


def test_two_labels_are_left_as_a_split(tmp_path):
    controller, _ = _controller(tmp_path, [_avg_metric()])
    slots = {"metric": "m_avg_score", "filters": {}}
    controller._salvage_derived_filter("延迟订单与准时订单的平均评分", slots)
    assert slots["filters"] == {}


def test_single_label_on_plain_metric_still_filters(tmp_path):
    controller, _ = _controller(tmp_path, [_avg_metric()])
    slots = {"metric": "m_avg_score", "filters": {}}
    controller._salvage_derived_filter("延迟订单的平均评分", slots)
    assert slots["filters"]["d_delay"] == {"op": "eq", "value": "延迟"}
