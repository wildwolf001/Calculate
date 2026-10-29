"""Regression coverage for comparison contribution semantics and composition."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain import checks as checks_mod
from app.domain.executor import ResultSet
from app.domain.plan import ComparisonSpec, QueryPlan, TimeRange
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer
from app.llm.parse_intent import LLMIntentParser


def _run(controller, session_id="s1"):
    async def consume():
        return [
            event async for event in controller.run_turn(
                session_id, "比较品类增长贡献"
            )
        ]

    return asyncio.run(consume())


def _metric_by_name(sem, name):
    return next(metric for metric in sem.metrics.values() if metric.name == name)


def _dimension_by_name(sem, name):
    return next(
        dim for dim in sem.dimensions.values()
        if dim.raw.get("name_zh") == name
    )


class ComparisonParser:
    def __init__(self, metric_id, dimension_id):
        self.metric_id = metric_id
        self.dimension_id = dimension_id

    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="A",
            confidence=1.0,
            slots={
                "metric": self.metric_id,
                "dimensions": [self.dimension_id],
                "comparison": {
                    "label": "2025年4月 vs 2025年3月",
                    "current": {"start": "2025-04-01", "end": "2025-05-01"},
                    "baseline": {"start": "2025-03-01", "end": "2025-04-01"},
                },
                "order_by": [{"field": "delta", "direction": "desc"}],
                "limit": 5,
            },
        )


def test_comparison_contribution_is_validated_and_named(tmp_path, sem):
    metric = _metric_by_name(sem, "已送达商品销售额")
    dimension = _dimension_by_name(sem, "品类")
    store = EventStore(tmp_path)
    controller = Controller(
        store, sem, ComparisonParser(metric.id, dimension.id),
        RuleGrounder(sem), TemplateComposer()
    )
    events = _run(controller)
    state = store.replay("s1")

    assert state.status is Status.DONE
    check_event = next(e for e in events if e.type == "checks.completed")
    issues = check_event.payload["issues"]
    assert not any(issue["id"] == "SHARE_CHECK_FAILED" for issue in issues)
    assert not any(issue["id"] == "COMPARISON_CHECK_FAILED" for issue in issues)

    facts = check_event.payload["facts"]
    assert facts["top_delta_member"]["value"] == "数码家电"
    assert facts["top_delta_member"]["delta"] == pytest.approx(5936.61)
    assert facts["comparison_delta_total"] == pytest.approx(2284.76)
    assert facts["top_contribution_share"] == pytest.approx(2.5984, rel=1e-4)

    answer = state.messages[-1]["text"]
    assert "数码家电" in answer
    assert "5,936.61" in answer
    assert "'value' is not in list" not in answer


def test_parser_normalizes_shorthand_compare_slot(sem):
    metric = _metric_by_name(sem, "已送达商品销售额")
    slots = LLMIntentParser(sem)._validate_slots({
        "metric": metric.id,
        "compare": "prev_period",
    })
    assert slots["compare"] == {"type": "prev_period"}


def test_delta_contribution_wins_over_current_size(monkeypatch, sem):
    metric = _metric_by_name(sem, "商品销售额")
    dimension = _dimension_by_name(sem, "品类")
    plan = QueryPlan(
        metric=metric.id,
        dimensions=[dimension.id],
        time=TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1)),
        comparison=ComparisonSpec(
            label="本期 vs 基期",
            current=TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1)),
            baseline=TimeRange(start=date(2025, 3, 1), end=date(2025, 4, 1)),
        ),
        order_by=[{"field": "delta", "direction": "desc"}],
    )
    result = ResultSet(
        result_id="r-test",
        columns=["category", "current", "baseline", "delta", "growth_rate"],
        column_types=["VARCHAR", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE"],
        rows=[
            ["规模最大", 100.0, 95.0, 5.0, 5.0 / 95.0],
            ["贡献最大", 80.0, 50.0, 30.0, 0.6],
        ],
        row_count=2,
        truncated=False,
        sql="",
        params=[],
        fingerprint="",
        duration_ms=0,
        meta={
            "current_range": ["2025-04-01", "2025-05-01"],
            "baseline_range": ["2025-03-01", "2025-04-01"],
        },
    )

    def fake_total(plan, metric, sem, db_path, time=None):
        return 180.0 if time and time.start.month == 4 else 145.0

    monkeypatch.setattr(checks_mod, "_metric_total", fake_total)
    issues, facts = checks_mod.run_checks(
        plan, metric, result, sem, quality={"checks": []}
    )
    assert not any(issue.id == "SHARE_CHECK_FAILED" for issue in issues)
    assert facts["top_delta_member"]["value"] == "贡献最大"
    assert facts["top_delta_member"]["delta"] == pytest.approx(30.0)


class MultiParser:
    def __init__(self, metric_ids):
        self.metric_ids = metric_ids

    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="B",
            confidence=1.0,
            subquestions=[
                {
                    "question": "总销售额是多少？",
                    "intent_type": "query",
                    "slots": {
                        "metric": self.metric_ids[0],
                        "dimensions": [],
                        "coverage_period": None,
                    },
                },
                {
                    "question": "商品数是多少？",
                    "intent_type": "query",
                    "slots": {
                        "metric": self.metric_ids[1],
                        "dimensions": [],
                        "coverage_period": None,
                    },
                },
                {
                    "question": "利润是多少？",
                    "intent_type": "query",
                    "slots": {"metric": "not_a_metric", "dimensions": []},
                },
            ],
        )


class RecordingComposer(TemplateComposer):
    def __init__(self):
        self.contexts = []

    def compose(self, ctx):
        self.contexts.append(ctx)
        return super().compose(ctx)


def test_multi_question_uses_one_structured_composition(tmp_path, sem):
    composer = RecordingComposer()
    metrics = [
        metric for metric in sem.metrics.values()
        if metric.raw.get("selected", True) and metric.type == "aggregate"
    ][:2]
    assert len(metrics) == 2
    store = EventStore(tmp_path)
    controller = Controller(
        store, sem, MultiParser([metric.id for metric in metrics]),
        RuleGrounder(sem), composer
    )
    _run(controller)

    assert len(composer.contexts) == 1
    context = composer.contexts[0]
    assert context["kind"] == "subquestions"
    assert [item["status"] for item in context["subquestions"]] == [
        "query", "query", "failed"
    ]
    assert all(
        "facts" in item["context"]
        for item in context["subquestions"] if item["status"] == "query"
    )
    answer = store.replay("s1").messages[-1]["text"]
    assert "not_a_metric" not in answer
    assert "补充明确的指标" in answer
