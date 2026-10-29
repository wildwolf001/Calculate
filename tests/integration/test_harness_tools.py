"""Integration coverage for the bounded tool harness."""
from __future__ import annotations

import asyncio

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer


def _metric_by_name(sem, name):
    return next(metric for metric in sem.metrics.values() if metric.name == name)


def _dimension_by_name(sem, name):
    return next(
        dim for dim in sem.dimensions.values()
        if dim.raw.get("name_zh") == name
    )


def _run(controller, text):
    async def consume():
        return [event async for event in controller.run_turn("s1", text)]

    return asyncio.run(consume())


class CountMembersParser:
    def __init__(self, metric_id):
        self.metric_id = metric_id

    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="A",
            confidence=1.0,
            slots={
                "metric": self.metric_id,
                "dimensions": [],
                "coverage_period": None,
            },
        )


def test_count_result_includes_database_members(tmp_path, sem):
    metric = _metric_by_name(sem, "品类数")
    store = EventStore(tmp_path)
    controller = Controller(
        store, sem, CountMembersParser(metric.id), RuleGrounder(sem),
        TemplateComposer(),
    )
    events = _run(controller, "一共有几类")
    state = store.replay("s1")

    assert state.status is Status.DONE
    checks = next(event for event in events if event.type == "checks.completed")
    facts = checks.payload["facts"]
    assert len(facts["member_values"]) == 6
    assert facts["member_values_source"] == "database"
    assert any(
        event.type == "tool.called"
        and event.payload["name"] == "members.lookup"
        for event in events
    )
    answer = state.messages[-1]["text"]
    for name in facts["member_values"]:
        assert name in answer


class NestedRatioParser:
    def __init__(self, metric_id, dimension_id):
        self.metric_id = metric_id
        self.dimension_id = dimension_id

    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="B",
            confidence=1.0,
            slots={
                "metric": self.metric_id,
                "dimensions": [self.dimension_id],
                "time": {"start": "2025-05-01", "end": "2025-06-01"},
                "nested": {"min_orders": 10},
                "order_by": [{"field": "value", "direction": "desc"}],
                "limit": 1,
            },
        )


def test_nested_ratio_is_repaired_to_having(tmp_path, sem):
    metric = _metric_by_name(sem, "延迟率")
    dimension = _dimension_by_name(sem, "商家名称")
    store = EventStore(tmp_path)
    controller = Controller(
        store, sem, NestedRatioParser(metric.id, dimension.id),
        RuleGrounder(sem), TemplateComposer(),
    )
    events = _run(controller, "5月已送达订单不少于10笔的商家中，延迟率最高的是谁？")
    state = store.replay("s1")

    assert state.status is Status.DONE
    assert any(event.type == "plan.repaired" for event in events)
    executed = next(event for event in events if event.type == "sql.executed")
    assert "HAVING" in executed.payload["sql"]
    assert executed.payload["rows"][0][-2] == 0.915254
    assert not any(event.type == "plan.rejected" for event in events)


class NullCoverageInterpretationParser:
    def __init__(self, metric_id):
        self.metric_id = metric_id

    def parse(self, text, context):
        slots = {
            "metric": self.metric_id,
            "dimensions": [],
            "coverage_period": None,
        }
        return ParseResult(
            intent_type="query",
            tier="B",
            confidence=0.9,
            slots=slots,
            interpretations=[
                {
                    "id": "first",
                    "label": "复购客户数",
                    "confidence": 0.9,
                    "slots": dict(slots),
                    "preferred": True,
                }
            ],
            preferred_interpretation="first",
        )


def test_interpretation_null_defaults_are_normalized(tmp_path, sem):
    metric = _metric_by_name(sem, "复购客户数")
    store = EventStore(tmp_path)
    controller = Controller(
        store, sem, NullCoverageInterpretationParser(metric.id),
        RuleGrounder(sem), TemplateComposer(),
    )
    events = _run(controller, "有多少自然客户完成了至少2笔已送达订单？")
    state = store.replay("s1")

    assert state.status is Status.DONE
    assert not any(event.type == "interpretation.rejected" for event in events)
    executed = next(event for event in events if event.type == "sql.executed")
    assert executed.payload["rows"] == [[2378]]


class RepairingParser:
    def __init__(self, bad_metric, good_metric):
        self.bad_metric = bad_metric
        self.good_metric = good_metric
        self.repair_calls = 0

    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="B",
            confidence=0.9,
            slots={
                "metric": self.bad_metric,
                "dimensions": [],
                "having": [
                    {"metric": "not_available", "op": "gte", "value": 1}
                ],
            },
        )

    def repair(self, text, context, *, plan, issues, tool_results):
        self.repair_calls += 1
        return ParseResult(
            intent_type="query",
            tier="A",
            confidence=1.0,
            slots={"metric": self.good_metric, "dimensions": []},
            notes=["repair called"],
        )


def test_llm_repair_hook_replans_after_validation_error(tmp_path, sem):
    bad = _metric_by_name(sem, "商品销售额")
    good = _metric_by_name(sem, "已送达订单数")
    parser = RepairingParser(bad.id, good.id)
    store = EventStore(tmp_path)
    controller = Controller(
        store, sem, parser, RuleGrounder(sem), TemplateComposer()
    )
    events = _run(controller, "已送达订单有多少？")
    state = store.replay("s1")

    assert parser.repair_calls == 1
    assert any(event.type == "plan.repair_requested" for event in events)
    assert not any(event.type == "plan.rejected" for event in events)
    assert state.status is Status.DONE
