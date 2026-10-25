"""Refusals must tell the user what *can* be asked in the current context."""
from __future__ import annotations

import asyncio

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.domain.plan import FilterSpec
from app.domain.resolver import RuleGrounder
from app.domain.pipeline import run_query
from app.domain.planner import plan_from_slots
from app.llm.compose_answer import TemplateComposer

T = {"start": "2025-05-01", "end": "2025-06-01"}


class UnsupportedParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="unsupported", tier="C", confidence=0.9,
            slots={
                "metric": "paid_amount",
                "filters": {"product": {"op": "eq", "value": "纯棉四件套15款"}},
                "time": T,
            },
            explanation="支付金额是订单级指标，无法按商品拆分。",
        )


class UnresolvedParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="B", confidence=0.7,
            slots={
                "metric": "gmv",
                "filters": {"product": {"op": "eq", "value": "纯棉四件套15款"}},
                "time": T,
            },
            unresolved=["利润"],
        )


def run(tmp_path, sem, parser, text):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    async def _consume():
        return [ev async for ev in ctrl.run_turn("s1", text)]
    return asyncio.run(_consume())


class SellerFollowupParser:
    def parse(self, text, context):
        slots = {
            "metric": "gmv", "dimensions": ["product"], "time": T,
            "filters": {"seller": {"op": "eq", "value": "广州美妆个护商家004"}},
            "order_by": [{"field": "value", "direction": "desc"}], "limit": 10,
        }
        return ParseResult(
            intent_type="query", tier="B", confidence=0.8, slots=slots,
            interpretations=[
                {"id": "by_product", "label": "按商品", "confidence": 0.66, "slots": dict(slots)},
                {"id": "by_category", "label": "按品类", "confidence": 0.34,
                 "slots": {**slots, "dimensions": ["category"]}},
            ],
            preferred_interpretation="by_product",
        )


def test_single_id_list_with_eq_filter_executes(tmp_path, sem):
    """Regression: grounding returns ['S004'] with op=eq; DuckDB must get IN (?) not = ?."""
    events = run(tmp_path, sem, SellerFollowupParser(), "这家主要卖了什么？")
    executed = [e.payload for e in events if e.type == "sql.executed"]
    assert len(executed) == 2
    assert all(p["row_count"] > 0 for p in executed)
    assert not any(e.type == "error.raised" for e in events)
    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "无法生成可执行的查询" not in answer


def test_freight_metric_is_available_at_product_level(sem):
    from app.core.state import Slots as CoreSlots
    plan = plan_from_slots(CoreSlots(
        metric="freight_total", dimensions=["product"],
        filters={"product": FilterSpec(op="eq", value="P0001")},
    ), sem)
    out = run_query(plan, sem)
    assert out.ok
    assert out.result is not None and out.result.rows[0][-1] > 0


def test_unsupported_answer_lists_contextual_questions(tmp_path, sem):
    events = run(tmp_path, sem, UnsupportedParser(), "纯棉四件套15款的支付金额是多少")
    payload = next(e.payload for e in events if e.type == "answer.composed")
    assert "纯棉四件套15款的商品销售额是多少？" in payload["options"]
    assert "2025年5月全部已送达订单的支付金额是多少？" in payload["options"]
    assert "可以这样问" in payload["text"]
    assert "你是想看" in payload["text"]


class PaidByProductParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="B", confidence=0.9,
            slots={
                "metric": "paid_amount", "dimensions": [],
                "filters": {"product": {"op": "eq", "value": "纯棉四件套15款"}},
                "time": T,
            },
        )


def test_order_level_metric_rejects_item_filter_with_repair(tmp_path, sem):
    events = run(tmp_path, sem, PaidByProductParser(), "纯棉四件套15款的支付金额是多少")
    types = [e.type for e in events]
    assert "plan.rejected" in types
    payload = next(e.payload for e in events if e.type == "answer.composed")
    assert "不支持按「商品」筛选" in payload["text"]
    assert "纯棉四件套15款的商品销售额是多少？" in payload["options"]
    assert "全部已送达订单的支付金额是多少？" in payload["text"]


class MonthProfitParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="unsupported", tier="C", confidence=0.9,
            slots={}, explanation="数据中没有成本字段，无法计算利润。",
        )


def test_time_only_context_repair_does_not_crash(tmp_path, sem):
    """Regression: RuleParser returns time as (date, date); repair must not .get it."""
    events = run(tmp_path, sem, MonthProfitParser(), "四月份利润是多少")
    payload = next(e.payload for e in events if e.type == "answer.composed")
    assert "2025年4月" in payload["text"]
    assert payload["options"]
    assert not any(e.type == "error.raised" for e in events)


def test_handler_exception_is_not_leaked_to_user(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, UnsupportedParser(), RuleGrounder(sem), TemplateComposer())

    def boom(state):
        raise RuntimeError("internal secret stack")

    ctrl._handle_parsing = boom  # type: ignore[method-assign]
    async def _consume():
        return [ev async for ev in ctrl.run_turn("s1", "纯棉四件套15款的支付金额是多少")]
    events = asyncio.run(_consume())
    error = next(e.payload for e in events if e.type == "error.raised")
    assert error["code"] == "INTERNAL_ERROR"
    assert "internal secret stack" not in error["message"]
    assert "RuntimeError" not in error["message"]


def test_unresolved_answer_offers_clickable_options(tmp_path, sem):
    events = run(tmp_path, sem, UnresolvedParser(), "纯棉四件套15款的利润是多少")
    payload = next(e.payload for e in events if e.type == "ambiguity.detected")
    assert payload["candidates"]
    assert any("商品销售额" in q for q in payload["candidates"])
    assert "数据里没有" in payload["message"]
    assert payload["partial_slots"]["filters"]["product"]["value"] == "纯棉四件套15款"
