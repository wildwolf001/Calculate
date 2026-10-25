"""On-demand business analysis: deterministic, session-aware, no parser call."""
from __future__ import annotations

import asyncio

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.interfaces import ParseResult
from app.core.state import Status
from app.domain.resolver import RuleGrounder
from app.llm.compose_answer import TemplateComposer


class FocusParser:
    """Every question becomes a 4月、数码家电 GMV query (used to set focus)."""

    def __init__(self):
        self.calls = 0

    def parse(self, text, context):
        self.calls += 1
        return ParseResult(
            intent_type="query", tier="A", confidence=1.0,
            slots={
                "metric": "gmv", "dimensions": [],
                "time": {"start": "2025-04-01", "end": "2025-05-01"},
                "filters": {"category": {"op": "eq", "value": "数码家电"}},
            },
        )


async def _consume(ctrl, text):
    return [ev async for ev in ctrl.run_turn("s1", text)]


async def _consume_analysis(ctrl, options=None):
    return [ev async for ev in ctrl.run_analysis("s1", options)]


def make(tmp_path, sem):
    store = EventStore(tmp_path)
    parser = FocusParser()
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    return store, ctrl, parser


def test_analysis_cue_runs_all_sections_without_parser(tmp_path, sem):
    store, ctrl, parser = make(tmp_path, sem)
    events = asyncio.run(_consume(ctrl, "帮我做个经营分析"))

    assert parser.calls == 0  # short analysis requests never hit the LLM parser
    requested = next(e for e in events if e.type == "analysis.requested")
    assert len(requested.payload["items"]) == 9
    assert not [e for e in events if e.type == "analysis.section.failed"]

    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 9
    assert all(p["analysis_item"] for p in (e.payload for e in executed))

    answer = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "左侧结果区的「经营分析」标签" in answer
    assert "规则解读" in answer
    assert "经营分析报告" not in answer  # the full report belongs to the canvas view

    state = store.replay("s1")
    assert state.status is Status.DONE
    assert state.analysis and state.analysis["status"] == "done"
    assert state.analysis["writer"] == "template"
    report = state.analysis["report"]
    for marker in ("经营分析报告", "核心结论", "规模与趋势", "品类结构",
                   "增长驱动", "履约与支付", "口碑与客户", "数据说明"):
        assert marker in report


class AnalysisParser:
    def parse(self, text, context):
        return ParseResult(intent_type="analysis", tier="B", confidence=1.0, slots={})


def test_llm_analysis_intent_routes_to_the_report(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, AnalysisParser(), RuleGrounder(sem), TemplateComposer())
    events = asyncio.run(_consume(ctrl, "帮我看看最近经营得怎么样，有哪些值得关注的地方"))
    assert any(e.type == "analysis.requested" for e in events)
    assert len([e for e in events if e.type == "sql.executed"]) == 9
    assert store.replay("s1").status is Status.DONE


def test_specific_period_does_not_trigger_analysis(tmp_path, sem):
    store, ctrl, parser = make(tmp_path, sem)
    events = asyncio.run(_consume(ctrl, "4月经营分析"))
    assert parser.calls == 1
    assert not [e for e in events if e.type == "analysis.requested"]


def test_analysis_inherits_session_focus_and_keeps_structure_global(tmp_path, sem):
    store, ctrl, parser = make(tmp_path, sem)
    asyncio.run(_consume(ctrl, "数码家电4月商品销售额"))  # establish focus

    events = asyncio.run(_consume(ctrl, "经营情况怎么样"))
    requested = next(e.payload for e in events if e.type == "analysis.requested")
    assert requested["focus"]["filters"].get("category")
    assert "数码家电" in requested["focus"]["focus_label"]

    by_id = {e.payload["analysis_item"]: e.payload
             for e in events if e.type == "sql.executed"}
    assert by_id["an1"]["plan"]["filters"]           # trend is focused
    assert not by_id["an2"]["plan"]["filters"]      # category structure stays global
    assert not by_id["an6"]["plan"]["filters"]      # payment has no category grain

    state = store.replay("s1")
    report = state.analysis["report"]
    assert "会话聚焦" in report
    assert "按全量口径" in report
    assert state.status is Status.DONE


class FakeWriter:
    def write(self, ctx):
        ids = [s["id"] for s in ctx["sections"]]
        return {
            "core_conclusions": ["模型核心结论：变化主要由订单量驱动。"],
            "sections": {sid: f"模型解读 {sid}：当期与基期的差异需要结合口径理解。"
                         for sid in ids},
            "watchouts": ["模型关注：样本期较短，结论需结合更长周期复核。"],
        }


def test_llm_commentary_is_merged_into_report_and_chat(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, FocusParser(), RuleGrounder(sem),
                      TemplateComposer(), analysis_writer=FakeWriter())
    events = asyncio.run(_consume(ctrl, "经营分析"))
    done = next(e.payload for e in events if e.type == "analysis.completed")
    assert done["writer"] == "llm"
    assert "模型核心结论" in done["report"]
    assert "模型解读 an1" in done["report"]
    assert "## 需要关注" in done["report"]
    assert done["core_conclusions"] == ["模型核心结论：变化主要由订单量驱动。"]

    chat = next(e.payload["text"] for e in events if e.type == "answer.composed")
    assert "模型解读" in chat
    assert "模型核心结论" in chat
    assert "经营分析报告" not in chat


def test_analysis_report_cites_result_ids_and_definitions(tmp_path, sem):
    store, ctrl, _ = make(tmp_path, sem)
    events = asyncio.run(_consume(ctrl, "经营分析"))
    report = store.replay("s1").analysis["report"]
    assert "（结果 r1-an1-" in report
    assert "已送达订单的订单明细 price 合计" in report
    assert "比率类指标的「变化」为百分点" in report


# ---------------------------------------------------------------------------
# user-selected modules + custom analysis block
# ---------------------------------------------------------------------------

def test_selected_modules_only(tmp_path, sem):
    store, ctrl, parser = make(tmp_path, sem)
    events = asyncio.run(_consume_analysis(ctrl, {"sections": ["fulfillment"]}))

    assert parser.calls == 0
    requested = next(e.payload for e in events if e.type == "analysis.requested")
    assert requested["modules"] == ["fulfillment"]
    assert [i["id"] for i in requested["items"]] == ["an5"]
    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 1 and executed[0].payload["analysis_item"] == "an5"

    report = store.replay("s1").analysis["report"]
    assert "**本次关注**：履约" in report
    assert "规模与趋势" not in report


class CustomQueryParser:
    def __init__(self):
        self.calls = 0

    def parse(self, text, context):
        self.calls += 1
        return ParseResult(
            intent_type="query", tier="A", confidence=1.0,
            rewritten_question="4月各品类商品销售额",
            slots={
                "metric": "gmv", "dimensions": ["category"],
                "time": {"start": "2025-04-01", "end": "2025-05-01"},
                "order_by": [{"field": "value", "direction": "desc"}],
            },
        )


def test_custom_block_runs_when_no_preset_selected(tmp_path, sem):
    store = EventStore(tmp_path)
    parser = CustomQueryParser()
    ctrl = Controller(store, sem, parser, RuleGrounder(sem), TemplateComposer())
    events = asyncio.run(_consume_analysis(ctrl, {
        "sections": [],
        "custom_text": "看看4月各品类销售额",
        "custom_title": "自定义：品类销售额",
    }))

    requested = next(e.payload for e in events if e.type == "analysis.requested")
    assert requested["modules"] == []
    assert requested["custom_text"] == "看看4月各品类销售额"
    assert parser.calls == 1

    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 1
    payload = executed[0].payload
    assert payload["analysis_item"] == "cx1"
    assert payload["analysis_section"] == "自定义：品类销售额"

    state = store.replay("s1")
    assert state.status is Status.DONE
    report = state.analysis["report"]
    assert "**自定义分析**：看看4月各品类销售额" in report
    assert "**分析期间**：以自定义板块的查询时间为准" in report
    assert "自定义：品类销售额" in report
    assert "规模与趋势" not in report


class UnsupportedCustomParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="unsupported", tier="C", confidence=0.9,
            explanation="数据中没有促销字段，无法做因果归因。",
        )


def test_custom_unsupported_is_isolated(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, UnsupportedCustomParser(), RuleGrounder(sem),
                      TemplateComposer())
    events = asyncio.run(_consume_analysis(ctrl, {
        "sections": [], "custom_text": "看看促销带来的增长",
    }))
    assert not [e for e in events if e.type == "sql.executed"]
    state = store.replay("s1")
    assert state.status is Status.DONE
    assert "数据中没有促销字段" in state.analysis["report"]
    assert "本节未能生成" in state.analysis["report"]


class CustomSubParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="B", confidence=0.9,
            subquestions=[
                {"question": "4月各品类销售额", "intent_type": "query",
                 "slots": {"metric": "gmv", "dimensions": ["category"],
                           "time": {"start": "2025-04-01", "end": "2025-05-01"}}},
                {"question": "4月订单数", "intent_type": "query",
                 "slots": {"metric": "order_count",
                           "time": {"start": "2025-04-01", "end": "2025-05-01"}}},
            ],
        )


def test_custom_block_can_have_multiple_subquestions(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, CustomSubParser(), RuleGrounder(sem),
                      TemplateComposer())
    events = asyncio.run(_consume_analysis(ctrl, {
        "sections": [], "custom_text": "4月品类销售额和订单数",
    }))
    executed = [e for e in events if e.type == "sql.executed"]
    assert [e.payload["analysis_item"] for e in executed] == ["cx1", "cx2"]
    completed = next(e.payload for e in events if e.type == "analysis.completed")
    assert len(completed["sections"]) == 2
    assert all(s["section"].startswith("自定义") for s in completed["sections"])


class CustomLookupParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="lookup", tier="A", confidence=1.0,
            lookup_dim="category", slots={},
        )


def test_custom_lookup_block_renders_member_list(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, CustomLookupParser(), RuleGrounder(sem),
                      TemplateComposer())
    events = asyncio.run(_consume_analysis(ctrl, {
        "sections": [], "custom_text": "有哪些品类",
    }))
    executed = [e for e in events if e.type == "sql.executed"]
    assert len(executed) == 1
    assert executed[0].payload["plan"]["kind"] == "member_list"
    report = store.replay("s1").analysis["report"]
    assert "品类清单" in report
    assert "数码家电" in report


class UnresolvedCustomParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="C", confidence=0.8,
            slots={}, unresolved=["珠宝类商品"],
        )


def test_custom_unresolved_lists_available_categories(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, UnresolvedCustomParser(), RuleGrounder(sem),
                      TemplateComposer())
    events = asyncio.run(_consume_analysis(ctrl, {
        "sections": [], "custom_text": "具体分析一下珠宝类商品的变化趋势",
    }))
    assert not [e for e in events if e.type == "sql.executed"]
    report = store.replay("s1").analysis["report"]
    assert "当前数据可用品类" in report
    assert "数码家电" in report
    assert "具体商品名" in report


class MissingProductParser:
    def parse(self, text, context):
        return ParseResult(
            intent_type="query", tier="A", confidence=0.9,
            slots={
                "metric": "gmv", "dimensions": ["month"],
                "filters": {"product": {"op": "eq", "value": "珠宝"}},
            },
        )


def test_custom_missing_product_lists_supported_categories(tmp_path, sem):
    store = EventStore(tmp_path)
    ctrl = Controller(store, sem, MissingProductParser(), RuleGrounder(sem),
                      TemplateComposer())
    events = asyncio.run(_consume_analysis(ctrl, {
        "sections": [], "custom_text": "分析珠宝类商品的变化趋势",
    }))
    assert not [e for e in events if e.type == "sql.executed"]
    report = store.replay("s1").analysis["report"]
    assert "数据里找不到" in report
    assert "当前数据可用品类" in report
    assert "数码家电" in report
