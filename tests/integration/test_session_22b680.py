"""SESSION 22B680 Turn 6 regression: resolver must not replace AVG with COUNT.

The raw LLM output for this question was correct:
`m_llm_已交付订单平均评分`, grouped by the derived delay dimension. The
deterministic resolver then rewrote it to `m_doc_延迟订单` because the group
label "延迟订单" scored higher than the synonym "平均评分". This test pins the
whole parse -> plan -> answer chain.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.datasets.registry import DatasetRegistry
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import Metric, SemanticLayer
from app.domain.semantic.resolver import (
    measure_kind,
    metric_text_support,
    requested_measure,
)
from app.llm.compose_answer import TemplateComposer
from app.llm.parse_intent import LLMIntentParser

DATASET_ID = "ds_d7390c8574a4"
DATASET = Path(__file__).resolve().parents[2] / "data" / "datasets" / DATASET_ID
QUESTION = "有评价的已送达订单中，延迟订单与准时订单的平均评分分别是多少？"

RAW = {
    "intent_type": "query",
    "tier": "A",
    "confidence": 0.95,
    "context": {"action": "new", "source": "none", "confidence": 1.0},
    "rewritten_question": "在有评价且已送达的订单中，按是否延迟分组，分别计算平均评价评分",
    "lookup_dimension": None,
    "slots": {
        "metric": "m_llm_已交付订单平均评分",
        "metric_expr": None,
        "metric_source": "documented",
        "ratio_numerator": None,
        "ratio_denominator": None,
        "dimensions": ["d_doc_t_007_c07_c08"],
        "time": None,
        "time_axis": None,
        "filters": {},
        "having": [],
        "compare": None,
        "comparison": None,
        "order_by": [],
        "limit": 100,
        "limit_per_group": None,
        "share": False,
        "coverage_period": None,
        "entity_count": None,
        "nested": None,
        "intent": "query",
        "unresolved": [],
    },
    "interpretations": [],
    "preferred": None,
    "ambiguity_note": None,
    "visualization": {
        "type": "bar", "x": "是否延迟", "y": "平均评分",
        "reason": "两个分类的指标对比",
    },
    "assumptions": [],
    "needs_clarification": False,
    "clarification_question": None,
    "subquestions": [],
    "notes": [],
}


class FakeClient:
    available = True

    def complete_json(self, *args, **kwargs):
        return copy.deepcopy(RAW)


def _sem() -> SemanticLayer:
    def metric(mid: str, raw: dict) -> Metric:
        return Metric(mid, raw)

    return SemanticLayer(
        version=1,
        dataset="fixture",
        tables={
            "t": {
                "grain": ["c01"],
                "time": "c04",
                "columns": ["c01", "c03", "c04", "c07", "c08"],
            }
        },
        joins={},
        pre_aggregates={},
        metrics={
            "m_delay": metric("m_delay", {
                "name_zh": "延迟订单",
                "type": "aggregate",
                "fact": "t",
                "expr": "COUNT(CASE WHEN t.c07 > t.c08 THEN 1 END)",
                "synonyms": ["延迟订单", "延迟订单数"],
                "selected": True,
            }),
            "m_avg": metric("m_avg", {
                "name_zh": "已交付订单平均评分",
                "type": "aggregate",
                "fact": "t",
                "expr": "AVG(t.c03)",
                "synonyms": ["平均评分", "评价得分"],
                "selected": True,
            }),
            "m_sum": metric("m_sum", {
                "name_zh": "销售额",
                "type": "aggregate",
                "fact": "t",
                "expr": "SUM(t.c06)",
                "synonyms": ["销售额", "销售金额"],
                "selected": True,
            }),
            "m_refund": metric("m_refund", {
                "name_zh": "退款率",
                "type": "ratio",
                "fact": "t",
                "numerator": "COUNT(CASE WHEN t.c03 = 'refunded' THEN 1 END)",
                "denominator": "COUNT(DISTINCT t.c01)",
                "synonyms": ["退款率"],
                "selected": True,
            }),
        },
        dimensions={},
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        capabilities={},
        raw={},
        time_axes=[],
    )


def _parser(sem: SemanticLayer) -> LLMIntentParser:
    parser = object.__new__(LLMIntentParser)
    parser.sem = sem
    parser.resolver = __import__(
        "app.domain.semantic.resolver", fromlist=["SemanticResolver"]
    ).SemanticResolver(sem)
    return parser


@pytest.mark.skipif(not DATASET.exists(), reason="SESSION 22B680 dataset is not available")
def test_turn6_keeps_average_rating_metric_and_returns_average(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    parser = LLMIntentParser(
        ctx.sem,
        client=FakeClient(),
        dim_values_path=ctx.dim_values_path,
        index=ctx.index,
        use_brief=True,
        model_card=ctx.model_card,
    )
    parsed = parser.parse(QUESTION, {"conversation": {}, "dataset": {}})
    assert parsed.slots["metric"] == "m_llm_已交付订单平均评分"
    assert parsed.slots["dimensions"] == ["d_doc_t_007_c07_c08"]

    controller = Controller(
        EventStore(tmp_path / "events"),
        ctx.sem, parser, RuleGrounder(ctx.sem, ctx.dim_values_path),
        TemplateComposer(), ctx=ctx,
    )
    controller.warehouse = ctx.warehouse

    async def run():
        return [event async for event in controller.run_turn("t6", QUESTION)]

    events = asyncio.run(run())
    executed = next(event.payload for event in events if event.type == "sql.executed")
    rows = {row[0]: row[1] for row in executed["rows"]}
    assert executed["plan"]["metric"] == "m_llm_已交付订单平均评分"
    assert rows["准时"] == pytest.approx(4.0)
    assert rows["延迟"] == pytest.approx(1.833333, abs=1e-5)

    checks = next(event.payload for event in events if event.type == "checks.completed")
    quality = [
        issue for issue in checks.get("issues") or []
        if issue.get("id") == "DATA_QUALITY"
    ]
    assert not quality, (
        "delivered rows have no null delivered_at; the table-wide 21 nulls "
        "are excluded by the current filter"
    )


def test_requested_measure_prioritises_average_over_generic_how_many():
    assert requested_measure(QUESTION) == "avg"
    assert requested_measure("延迟订单有多少") == "count"
    assert requested_measure("销售额是多少") == "sum"
    assert requested_measure("延迟率最高的是谁") == "ratio"


def test_metric_kind_and_text_support():
    sem = _sem()
    assert measure_kind(sem.metrics["m_delay"]) == "count"
    assert measure_kind(sem.metrics["m_avg"]) == "avg"
    assert measure_kind(sem.metrics["m_sum"]) == "sum"
    assert measure_kind(sem.metrics["m_refund"]) == "ratio"
    assert metric_text_support(sem.metrics["m_avg"], QUESTION)
    assert not metric_text_support(sem.metrics["m_avg"], "延迟订单有多少")


def test_average_is_not_overridden_by_delay_count():
    parser = _parser(_sem())
    result = ParseResult(
        intent_type="query",
        slots={"metric": "m_avg", "dimensions": ["d_delay"], "filters": {}},
    )
    parsed = parser._apply_resolver(QUESTION, result)
    assert parsed.slots["metric"] == "m_avg"


def test_count_question_keeps_count_metric():
    parser = _parser(_sem())
    result = ParseResult(
        intent_type="query",
        slots={"metric": "m_delay", "dimensions": [], "filters": {}},
    )
    parsed = parser._apply_resolver("延迟订单有多少", result)
    assert parsed.slots["metric"] == "m_delay"


def test_average_question_replaces_count_metric():
    parser = _parser(_sem())
    result = ParseResult(
        intent_type="query",
        slots={"metric": "m_delay", "dimensions": [], "filters": {}},
    )
    parsed = parser._apply_resolver("延迟订单的平均评分是多少", result)
    assert parsed.slots["metric"] == "m_avg"


def test_sum_question_replaces_unrelated_metric():
    parser = _parser(_sem())
    result = ParseResult(
        intent_type="query",
        slots={"metric": "m_refund", "dimensions": [], "filters": {}},
    )
    parsed = parser._apply_resolver("销售额是多少", result)
    assert parsed.slots["metric"] == "m_sum"


@pytest.mark.skipif(not DATASET.exists(), reason="SESSION 22B680 dataset is not available")
def test_new_metric_without_time_does_not_inherit_previous_month():
    controller = object.__new__(Controller)
    result = ParseResult(
        intent_type="query",
        slots={
            "metric": "m_llm_已交付订单平均评分",
            "time": {"start": "2025-05-01", "end": "2025-06-01"},
            "comparison": {"label": "5月 vs 4月"},
        },
    )
    controller._drop_spurious_inherited_time(
        result,
        "有评价的已送达订单中，延迟订单与准时订单的平均评分分别是多少？",
        {"active_scope": {"metric": "m_llm_延迟交付率"}},
    )
    assert result.slots["time"] is None
    assert result.slots["comparison"] is None


@pytest.mark.skipif(not DATASET.exists(), reason="SESSION 22B680 dataset is not available")
def test_new_metric_with_explicit_month_keeps_that_month():
    controller = object.__new__(Controller)
    result = ParseResult(
        intent_type="query",
        slots={
            "metric": "m_llm_已交付订单平均评分",
            "time": {"start": "2025-05-01", "end": "2025-06-01"},
        },
    )
    controller._drop_spurious_inherited_time(
        result,
        "5月有评价的已交付订单平均评分是多少？",
        {"active_scope": {"metric": "m_llm_延迟交付率"}},
    )
    assert result.slots["time"] == {"start": "2025-05-01", "end": "2025-06-01"}


@pytest.mark.skipif(not DATASET.exists(), reason="SESSION 22B680 dataset is not available")
def test_q8_schema_join_question_gets_deterministic_explanation(tmp_path):
    class UnsupportedParser:
        def parse(self, text, context):
            return ParseResult(intent_type="unsupported", tier="C", confidence=0.1)

    ctx = DatasetRegistry().get(DATASET_ID)
    controller = Controller(
        EventStore(tmp_path / "q8-events"),
        ctx.sem, UnsupportedParser(), RuleGrounder(ctx.sem, ctx.dim_values_path),
        TemplateComposer(), ctx=ctx,
    )

    async def run():
        return [
            event async for event in controller.run_turn(
                "q8", "直接关联商品明细和支付表为什么会导致金额重复？"
            )
        ]

    events = asyncio.run(run())
    answer = next(event.payload["text"] for event in events if event.type == "answer.composed")
    assert "order_items" in answer
    assert "order_payments" in answer
    assert "放大" in answer
    assert not any(event.type == "sql.executed" for event in events)


def test_schema_join_explanation_does_not_invent_order_tables():
    class Ctx:
        model_card = {
            "tables": {
                "subscriptions": {
                    "label": "subscriptions",
                    "grain": ["account_id", "plan_id"],
                },
                "payments": {
                    "label": "payments",
                    "grain": ["account_id", "payment_id"],
                },
            }
        }

    class Sem:
        tables = {"subscriptions": {}, "payments": {}}
        dimensions: dict = {}

    class FakeController:
        ctx = Ctx()
        sem = Sem()

    FakeController._JOIN_EXPLANATION_RE = Controller._JOIN_EXPLANATION_RE
    answer = Controller._schema_join_explanation(
        FakeController(),
        "直接将订阅表和支付表关联为什么会导致金额重复？",
    )
    assert answer
    assert "order_items" not in answer
    assert "order_payments" not in answer
    assert "t_004" not in answer
    assert "t_005" not in answer
