"""SESSION 2FF8B7 UX regression: refusals, vague analysis, bad interpretations.

Three user-visible problems:
- prediction/profit refusals leaked physical column names and generic word soup;
- "一共卖了多少" became an internal error when one interpretation was invalid;
- "卖得怎么样" silently ran a 12-query analysis report.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.datasets.registry import DatasetRegistry
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer

DATASET_ID = "ds_dd0addc20f5c"
DATASET = Path(__file__).resolve().parents[2] / "data" / "datasets" / DATASET_ID

pytestmark = pytest.mark.skipif(
    not (DATASET / "semantic.yaml").exists(),
    reason="SESSION 2FF8B7 dataset is not available",
)


class FailParser:
    def parse(self, text, context):
        raise AssertionError("refusal/analysis guard should run before the LLM parser")


class AnalysisParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="analysis",
            tier="B",
            confidence=0.72,
            slots={},
            rewritten_question="整体卖得怎么样？",
        )


class MultiParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query",
            tier="A",
            confidence=0.8,
            slots={"metric": "m_004_c06_sum", "dimensions": []},
            interpretations=[
                {
                    "id": "sales",
                    "label": "商品销售额",
                    "confidence": 0.55,
                    "slots": {"metric": "m_004_c06_sum", "dimensions": []},
                },
                {
                    "id": "orders",
                    "label": "订单数",
                    "confidence": 0.3,
                    "slots": {"metric": "m_003_c01_distinct", "dimensions": []},
                },
                {
                    "id": "bad_dimension",
                    "label": "无效维度组合",
                    "confidence": 0.15,
                    # A dimension id used as a scope table makes the compiler
                    # raise SemanticError ("需要的表 d_003_c03 ... 无法关联").
                    "slots": {
                        "metric": "bad_metric",
                        "metric_spec": {
                            "id": "bad_metric",
                            "name": "坏口径",
                            "fact": "t_004",
                            "expr": "COUNT(*)",
                            "scope": {"d_003_c03": "delivered"},
                        },
                    },
                },
            ],
            preferred_interpretation="sales",
            ambiguity_note="一句话有多种统计口径",
        )


def _events(ctx, parser, text, tmp_path):
    controller = Controller(
        EventStore(tmp_path / "events"), ctx.sem, parser,
        RuleGrounder(ctx.sem, ctx.dim_values_path), TemplateComposer(), ctx=ctx,
    )
    controller.warehouse = ctx.warehouse

    async def run():
        return [event async for event in controller.run_turn("s-2ff8b7", text)]

    return asyncio.run(run())


def test_prediction_refusal_is_specific_and_chinese(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    events = _events(ctx, FailParser(), "能预测下个月卖多少吗？", tmp_path)
    text = next(event.payload["text"] for event in events
                if event.type == "answer.composed")
    assert "不能" in text and "预测" in text
    assert "price" not in text
    assert "freight_value" not in text
    assert "历史基线" in text
    assert any(event.type == "result.presented" for event in events)


def test_profit_refusal_explains_missing_cost_data(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    events = _events(ctx, FailParser(), "利润多少？", tmp_path)
    text = next(event.payload["text"] for event in events
                if event.type == "answer.composed")
    assert "成本" in text and "利润" in text
    assert "商品销售额也不等于利润" in text
    assert "price" not in text
    assert "freight_value" not in text


def test_vague_performance_question_asks_scope_not_full_report(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    events = _events(ctx, AnalysisParser(), "卖得怎么样？", tmp_path)
    assert not any(event.type in {"analysis.requested", "analysis.completed"}
                   for event in events)
    ambiguity = next(event for event in events if event.type == "ambiguity.detected")
    assert ambiguity.payload["kind"] == "scope"
    assert len(ambiguity.payload["candidates"]) >= 3
    assert "经营报告" in ambiguity.payload["message"]


def test_bad_interpretation_does_not_kill_the_turn(tmp_path):
    ctx = DatasetRegistry().get(DATASET_ID)
    events = _events(ctx, MultiParser(), "一共卖了多少？", tmp_path)
    errors = [
        event.payload for event in events
        if event.type == "error.raised"
        and event.payload.get("code") == "INTERNAL_ERROR"
    ]
    assert not errors
    executed = [event for event in events if event.type == "sql.executed"]
    assert len(executed) >= 1
    rejected = [event for event in events if event.type == "interpretation.rejected"]
    assert any("bad_dimension" == event.payload.get("interpretation_id")
               for event in rejected)
    completed = next(event for event in events if event.type == "turn.completed")
    assert completed.payload["status"] == "DONE"
