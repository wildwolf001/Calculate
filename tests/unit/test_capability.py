from __future__ import annotations

from datetime import date

from app.core.state import Slots
from app.domain.capability import apply_degradations, assess_slots, render_refusal
from app.domain.plan import FilterSpec, TimeRange
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem(metrics=None, dimensions=None, joins=None, time_range=("2024-01-01", "2025-12-31")):
    return SemanticLayer(
        version=1,
        dataset="test",
        tables={
            "t1": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03"]},
            "t2": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
        },
        joins=joins or {},
        pre_aggregates={},
        metrics=metrics or {},
        dimensions=dimensions or {},
        policies={},
        time_range=time_range,
        binding={"tables": {}},
        raw={},
    )


def _metric(**overrides):
    raw = {
        "name_zh": "评分",
        "type": "aggregate",
        "fact": "t1",
        "expr": "AVG(t1.c03)",
        "time_via": "t1.c02",
        "supported_grain": ["none", "month", "d_status"],
    }
    raw.update(overrides)
    return Metric("m_score", raw)


def _dims():
    return {
        "d_status": Dimension("d_status", {
            "id": "d_status", "name_zh": "是否延迟", "kind": "derived",
            "requires": "t1",
        }),
        "d_city": Dimension("d_city", {"id": "d_city", "name_zh": "城市", "column": "t1.c03"}),
        "d_other": Dimension("d_other", {"id": "d_other", "name_zh": "外部维度", "column": "t2.c01"}),
    }


def test_answerable_report_is_not_refused():
    sem = _sem({"m_score": _metric()}, _dims())
    slots = Slots(metric="m_score", dimensions=["d_status"])
    report = assess_slots(slots, sem)
    assert report.status == "answerable"
    assert report.missing == []


def test_unknown_metric_is_refused_with_alternatives():
    sem = _sem({"m_score": _metric()}, _dims())
    slots = Slots(metric="m_missing")
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.missing[0].kind == "metric"
    assert report.refusal_payload()["code"] == "MISSING_METRIC"
    text = render_refusal(report, sem)
    assert "可用指标" in text
    assert "可以这样问" in text


def test_unsupported_dimension_degrades_instead_of_refusing():
    sem = _sem({"m_score": _metric()}, _dims())
    slots = Slots(metric="m_score", dimensions=["d_other"])
    report = assess_slots(slots, sem)
    assert report.status == "degradable"
    assert report.refused is False
    degraded = apply_degradations(slots, report)
    assert degraded.dimensions == []
    assert slots.dimensions == ["d_other"]  # original untouched


def test_time_window_outside_coverage_is_refused():
    sem = _sem({"m_score": _metric()}, _dims(), time_range=("2024-01-01", "2024-12-31"))
    slots = Slots(
        metric="m_score",
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
    )
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "NO_TIME_DATA"


def test_unknown_filter_dimension_is_refused():
    sem = _sem({"m_score": _metric()}, _dims())
    slots = Slots(metric="m_score", filters={"d_unknown": FilterSpec(op="eq", value="x")})
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "MISSING_FILTER"


def test_all_null_measure_is_refused_from_health():
    metric = _metric(data_health={"rows": 10, "measure_null_rate": 1.0})
    sem = _sem({"m_score": metric}, _dims())
    report = assess_slots(Slots(metric="m_score"), sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "DATA_UNAVAILABLE"


def test_planning_emits_capability_and_deterministic_refusal(tmp_path):
    from app.core.controller import Controller
    from app.core.event_store import EventStore
    from app.core.state import SessionState

    sem = _sem({"m_score": _metric()}, _dims())
    controller = Controller(EventStore(tmp_path / "sessions"), sem, None, None, None)
    state = SessionState(session_id="s-test")
    state.slots = Slots(metric="m_missing")

    events = controller._handle_planning(state)
    types = [event[0] for event in events]

    assert types[0] == "capability.assessed"
    assert types[1] == "refusal.decided"
    assert "answer.composed" in types
    composed = next(payload for name, payload in events if name == "answer.composed")
    assert "答不了" in composed["text"]
    assert composed["status"] == "refused"


def test_time_window_uses_per_metric_coverage():
    metric = _metric(data_health={"rows": 10, "time_coverage": ["2025-01-01", "2025-03-01"]})
    sem = _sem({"m_score": metric}, _dims(), time_range=("2024-01-01", "2025-12-31"))
    slots = Slots(
        metric="m_score",
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
    )
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "NO_TIME_DATA"


def test_salvage_maps_delivery_label_to_derived_filter(tmp_path):
    from app.core.controller import Controller
    from app.core.event_store import EventStore

    dims = _dims()
    dims["d_delivery"] = Dimension("d_delivery", {
        "id": "d_delivery", "name_zh": "是否延迟", "kind": "derived",
        "labels": {"true": "延迟", "false": "准时"},
    })
    sem = _sem({"m_score": _metric()}, dims)
    controller = Controller(EventStore(tmp_path / "sessions"), sem, None, None, None)

    slots = {"metric": "m_score", "filters": {}, "unresolved": ["延迟订单限定条件"]}
    controller._salvage_derived_filter("延迟订单的评价平均分", slots)
    assert slots["filters"]["d_delivery"]["value"] == "延迟"
    assert slots["unresolved"] == []


def test_scope_filter_conflict_is_refused():
    metric = _metric(scope={"t1.c03": "delivered"})
    sem = _sem({"m_score": metric}, _dims())
    slots = Slots(
        metric="m_score",
        filters={"d_city": FilterSpec(op="eq", value="cancelled")},
    )
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "MISSING_FILTER"


def test_time_clamp_is_applied():
    sem = _sem({"m_score": _metric()}, _dims(), time_range=("2024-01-01", "2025-12-31"))
    slots = Slots(
        metric="m_score",
        time=TimeRange(start=date(2023, 12, 1), end=date(2024, 2, 1)),
    )
    report = assess_slots(slots, sem)
    assert report.status == "degradable"
    clone = apply_degradations(slots, report)
    assert str(clone.time.start) == "2024-01-01"
    assert str(clone.time.end) == "2024-02-01"


def test_policy_can_refuse():
    from app.domain.policy import DenyAllPolicy

    sem = _sem({"m_score": _metric()}, _dims())
    slots = Slots(metric="m_score", dimensions=[])
    report = assess_slots(slots, sem, policy=DenyAllPolicy())
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "ACCESS_DENIED"
    assert report.ok is False if hasattr(report, "ok") else True


def test_fanout_dimension_is_refused_not_degraded():
    sem = _sem(
        {"m_score": _metric()}, _dims(),
        joins={"t2": {"t1": {"left": "c01", "right": "c01", "card": "N:1"}}},
    )
    slots = Slots(metric="m_score", dimensions=["d_other"])
    report = assess_slots(slots, sem)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "FANOUT_UNSAFE"
