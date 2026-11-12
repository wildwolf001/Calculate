"""The driving loop: state machine owns transitions, model is a subroutine.

Every step appends exactly one event. The loop re-reads the event log each
iteration (`reduce`), so an in-memory object can never drift from history.
"""
from __future__ import annotations

import json
import re
import threading
import traceback
from dataclasses import replace
from typing import AsyncIterator

from app.config.settings import (
    DIM_VALUES_PATH,
    MAX_STEPS,
    MAX_SUBQUESTIONS,
    WAREHOUSE_PATH,
)
from app.core.context import (
    apply_context_action,
    build_conversation_context,
    context_for_event,
    project_for_composer,
    project_for_parser,
)
from app.core.event_store import EventStore
from app.core.events import Event
from app.core.interfaces import AnswerComposer, IntentParser, ValueGrounder
from app.core.logging import log_event, logger
from app.core.memory import MemoryCompressor
from app.core.state import SessionState, Slots, Status
from app.core.trace import Tracer, new_trace_id
from app.domain.capability import (
    CapabilityRefusal,
    CapabilityReport,
    apply_degradations,
    assess_slots,
    render_refusal,
)
from app.domain.checks import CheckIssue
from app.domain.diagnosis import diagnose_empty
from app.domain.recovery import candidates_for_failure, classify_error
from app.domain.pipeline import (
    PipelineError,
    check,
    execute_prepared,
    prepare,
)
from app.domain.analysis import (
    build_analysis_items,
    custom_analysis_item,
    module_catalog,
)
from app.domain.insights import build_insights
from app.domain.planner import (
    degrade_unsupported_dimensions,
    member_list_plan,
    plan_from_ir,
)
from app.domain.plan import FilterSpec, HavingSpec, QueryPlan
from app.domain.policy import PolicySet
from app.domain.query.ir import QueryIR
from app.domain import lookup as lookup_mod
from app.domain.presentation import display_spec, stabilize_ratio_ranking, summarize_rows
from app.domain.visualization import recommend_chart
from app.domain.repair import build_repair, repair_text
from app.domain.rewrite import rewrite
from app.domain.semantic.loader import SemanticLayer, SemanticError
from app.llm.rule_parser import RuleParser
from app.llm.analyze import LLMAnalysisWriter
from app.llm.client import LLMTimeout, LLMTruncated, LLMUnavailable
from app.tools.harness import ToolHarness, ToolResult

SMALLTALK_TEXT = (
    "你好，我是明账。直接问经营问题就行，比如「4月销售额最高的品类」、"
    "「5月延迟率最高的商家」或「有多少复购客户」。"
)
UNSUPPORTED_SUGGESTIONS = (
    "可以问：商品销售额 / 订单数 / 支付金额 / 延迟率 / 平均评分 / 低分率 / "
    "复购客户数 / 客单价，并指定时间（如「2025年5月」）和维度（如「按品类」）。"
)

HANDLERS = {
    Status.PARSING: "PARSING",
    Status.GROUNDING: "GROUNDING",
    Status.PLANNING: "PLANNING",
    Status.VALIDATING: "VALIDATING",
    Status.EXECUTING: "EXECUTING",
    Status.CHECKING: "CHECKING",
    Status.COMPOSING: "COMPOSING",
}


class Controller:
    def __init__(self, store: EventStore, sem: SemanticLayer,
                 parser: IntentParser, grounder: ValueGrounder,
                 composer: AnswerComposer, tracer: Tracer | None = None,
                 ctx=None, analysis_writer: LLMAnalysisWriter | None = None,
                 policy: PolicySet | None = None,
                 memory_compressor: MemoryCompressor | None = None):
        self.store = store
        self.sem = ctx.sem if ctx is not None else sem
        self.parser = parser
        self.grounder = grounder
        self.composer = composer
        self.tracer = tracer
        self.ctx = ctx
        self.dataset_id = ctx.dataset_id if ctx is not None else None
        self.warehouse = ctx.warehouse if ctx is not None else WAREHOUSE_PATH
        self.dim_values_path = (
            ctx.dim_values_path if ctx is not None else DIM_VALUES_PATH
        )
        self.quality = ctx.quality if ctx is not None else None
        self.analysis_writer = analysis_writer or LLMAnalysisWriter()
        self.policy = policy or PolicySet()
        self.memory_compressor = memory_compressor
        self.tools = ToolHarness(self.sem, self.warehouse)
        self._results: dict[tuple[str, int], object] = {}
        self._cancel_requested: set[str] = set()

    def cancel(self, session_id: str) -> None:
        """Request cancellation at the next state-machine boundary."""
        self._cancel_requested.add(session_id)

    # ---------- public ----------
    async def run_turn(self, session_id: str, text: str) -> AsyncIterator[Event]:
        state = self.store.replay(session_id)
        turn = state.turn + 1
        trace_id = new_trace_id()

        yield self.store.append(session_id, "turn.started",
                                {"turn": turn}, trace_id=trace_id)
        yield self.store.append(session_id, "user.message",
                                {"text": text, "turn": turn}, trace_id=trace_id)
        state = self.store.replay(session_id)
        yield self.store.append(
            session_id,
            "context.compiled",
            context_for_event(build_conversation_context(state, self.sem)),
            trace_id=trace_id,
        )
        rw = rewrite(text)
        yield self.store.append(
            session_id,
            "query.normalized",
            {
                "original": rw.original,
                "normalized": rw.normalized,
                "changes": rw.changes,
                "kind": rw.kind,
                "followup": rw.followup,
            },
            trace_id=trace_id,
        )

        for _ in range(MAX_STEPS):
            state = self.store.replay(session_id)
            if session_id in self._cancel_requested:
                self._cancel_requested.discard(session_id)
                yield self.store.append(session_id, "turn.cancelled", {
                    "turn": turn,
                    "stage": state.status.value,
                    "message": "已停止本轮生成。",
                }, trace_id=trace_id)
                yield self.store.append(session_id, "answer.composed", {
                    "text": "已停止本轮生成。可以继续问下一个问题。",
                    "result_id": None,
                    "status": "cancelled",
                }, trace_id=trace_id)
                break
            if state.status in {Status.DONE, Status.FAILED, Status.AWAITING_CLARIFICATION}:
                break
            handler_name = HANDLERS.get(state.status)
            if handler_name is None:
                break
            handler = getattr(self, f"_handle_{handler_name.lower()}")
            try:
                with self._span(handler_name, trace_id, state):
                    events = handler(state)
            except Exception as exc:  # defensive: a handler must not kill the loop
                # Never leak stack/exception text to the user; log it instead.
                log_event(
                    logger, "handler.error", stage=handler_name,
                    error=f"{type(exc).__name__}: {exc}",
                    detail=traceback.format_exc()[:4000],
                )
                events = [("error.raised", {
                    "code": "INTERNAL_ERROR",
                    "message": "这一轮处理失败，系统内部出现异常。请换一种问法或稍后重试。",
                    "recoverable": False,
                })]
            for ev_type, payload in events:
                yield self.store.append(session_id, ev_type, payload,
                                        caused_by=f"seq:{state.last_seq}", trace_id=trace_id)
        else:
            yield self.store.append(session_id, "error.raised", {
                "code": "MAX_STEPS", "message": f"超过最大步数 {MAX_STEPS}",
                "recoverable": False,
            }, trace_id=trace_id)

        final = self.store.replay(session_id)
        memory = self._memory_payload(final, allow_llm=False)
        if memory:
            # Fast deterministic memory is available immediately; the model
            # summary is refreshed after the stream completes.
            self.store.append(
                session_id, "memory.compressed", memory,
                trace_id=trace_id,
            )
        self._schedule_memory_compression(session_id, final, trace_id)
        yield self.store.append(session_id, "turn.completed",
                                {"status": final.status.value, "turn": turn},
                                trace_id=trace_id)

    async def run_analysis(self, session_id: str,
                           options: dict | None = None) -> AsyncIterator[Event]:
        """Generate an analysis report inside the current dataset context."""
        state = self.store.replay(session_id)
        turn = state.turn + 1
        trace_id = new_trace_id()
        yield self.store.append(
            session_id, "turn.started",
            {"turn": turn, "kind": "analysis"}, trace_id=trace_id,
        )
        yield self.store.append(
            session_id, "user.message",
            {
                "text": self._analysis_request_text(options),
                "turn": turn,
                "kind": "analysis",
            },
            trace_id=trace_id,
        )
        state.turn = turn
        request_text = self._analysis_request_text(options)
        yield self.store.append(
            session_id,
            "context.compiled",
            context_for_event(build_conversation_context(state, self.sem)),
            trace_id=trace_id,
        )
        rw = rewrite(request_text)
        yield self.store.append(
            session_id,
            "query.normalized",
            {
                "original": rw.original,
                "normalized": rw.normalized,
                "changes": rw.changes,
                "kind": "analysis",
                "followup": False,
            },
            trace_id=trace_id,
        )
        try:
            events = self._analysis_events(state, options)
        except Exception as exc:
            log_event(
                logger, "analysis.failed",
                error=f"{type(exc).__name__}: {exc}",
                detail=traceback.format_exc()[:4000],
            )
            events = [("error.raised", {
                "code": "ANALYSIS_FAILED",
                "message": "经营分析生成失败。可以缩小范围或稍后重试。",
                "recoverable": False,
            })]
        for event_type, payload in events:
            yield self.store.append(
                session_id, event_type, payload,
                caused_by=f"seq:{state.last_seq}", trace_id=trace_id,
            )
        final = self.store.replay(session_id)
        yield self.store.append(
            session_id, "turn.completed",
            {"status": final.status.value, "turn": turn, "kind": "analysis"},
            trace_id=trace_id,
        )

    # ---------- helpers ----------
    def _span(self, name: str, trace_id: str, state: SessionState):
        if self.tracer is None:
            from contextlib import nullcontext
            return nullcontext()
        return self.tracer.span(f"controller.{name.lower()}", trace_id,
                                session_id=state.session_id, turn=state.turn, status=state.status.value)

    def _last_user_text(self, state: SessionState) -> str:
        for m in reversed(state.messages):
            if m["role"] == "user":
                return m["text"]
        return ""

    def _suggestions(self) -> str:
        metrics = [m for m in self.sem.metrics.values() if m.raw.get("selected", True)]
        names = [m.name for m in (metrics or list(self.sem.metrics.values()))[:10]]
        if not names:
            return "当前数据源还没有可查询的指标，请先在数据管理页确认语义草稿。"
        return "可以问：" + "、".join(names) + "，并指定时间或维度。"

    def _quality_fallback_text(self) -> str:
        checks = [
            item for item in ((self.quality or {}).get("checks") or [])
            if item.get("severity") in {"warn", "error"}
            and (item.get("count") or 0) > 0
        ]
        lines = [
            "当前数据集没有可用于时间覆盖诊断的时间列，"
            "因此无法给出按月的覆盖结论。"
        ]
        if checks:
            lines.append("已检测到的数据质量问题：")
            for item in checks[:8]:
                lines.append(f"- {item.get('title')}（{item.get('count')}）")
        lines.append(
            "建议先确认日期列的格式和范围；修正后再做月份覆盖/趋势分析。"
        )
        return "\n".join(lines)

    def _semantic_status(self) -> str:
        return self.ctx.mode if self.ctx is not None else "published"

    def _with_draft_notice(self, text: str) -> str:
        if self.ctx is not None and self.ctx.mode == "draft":
            return "【自动口径 · 未经业务确认】\n\n" + text
        return text

    def _repair_slots(self, state: SessionState, slots: dict | None) -> dict:
        """Fill entity/time for repair hints, preferring display names."""
        data = dict(slots or {})
        try:
            ctx = RuleParser(self.sem, self.dim_values_path).extract_context(
                self._last_user_text(state))
        except Exception:
            ctx = {}
        if ctx.get("filters"):
            data["filters"] = ctx["filters"]
        elif data.get("filters"):
            from app.core.context import humanize_slots
            data = humanize_slots(data, self.dim_values_path)
        if ctx.get("time"):
            data["time"] = ctx["time"]
        return data

    def _call_with_retry(self, stage: str, fn, state: SessionState, events: list):
        """One retry with an llm.retry event; re-raises on second failure.

        Timeouts and unavailable/truncated providers are not retried: retrying
        a 46s timeout turns a bad first attempt into a 90s turn. Let the
        deterministic parser/fallback handle those immediately.
        """
        try:
            return fn()
        except (LLMTimeout, LLMUnavailable, LLMTruncated):
            raise
        except Exception as exc:
            if state.policy.can_retry(stage):
                events.append(("llm.retry", {"stage": stage, "attempt": 1, "reason": str(exc)}))
                return fn()
            raise

    @staticmethod
    def _normalize_slots_payload(payload: dict) -> dict:
        defaults = Slots().model_dump()
        return {
            key: value
            for key, value in payload.items()
            if not (value is None and defaults.get(key) is not None)
        }

    @staticmethod
    def _tool_events(name: str, arguments: dict, result) -> list[tuple[str, dict]]:
        return [
            ("tool.called", {"name": name, "arguments": arguments}),
            ("tool.result", result.as_dict()),
        ]

    def _enrich_count_members(self, plan: QueryPlan, artifact,
                              facts: dict) -> list[tuple[str, dict]]:
        """Attach low-cardinality members to a count result from the database."""
        if (
            plan.dimensions
            or plan.filters
            or plan.compare
            or plan.comparison
            or artifact.row_count != 1
            or "value" not in artifact.columns
        ):
            return []
        dimension_id = self.tools.infer_count_member_dimension(plan.metric)
        if not dimension_id:
            return []
        idx = artifact.columns.index("value")
        raw_value = artifact.rows[0][idx]
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            return []
        if count < 0 or count > 2000:
            return []
        result = self.tools.lookup_members(dimension_id, limit=self.tools.max_members)
        if not result.ok:
            return self._tool_events(
                "members.lookup",
                {"dimension_id": dimension_id, "purpose": "expand_count"},
                result,
            )
        members = list((result.data or {}).get("matches") or [])
        facts["member_dimension"] = dimension_id
        facts["member_dimension_name"] = self.sem.dimension(
            dimension_id
        ).raw.get("name_zh", dimension_id)
        facts["member_values"] = members
        facts["member_values_truncated"] = bool(
            (result.data or {}).get("truncated")
        )
        facts["member_values_source"] = "database"
        return self._tool_events(
            "members.lookup",
            {
                "dimension_id": dimension_id,
                "purpose": "expand_count",
            },
            result,
        )

    # ---------- handlers ----------
    def _is_vague_analysis(self, text: str) -> bool:
        """Do not turn "卖得怎么样" into a 12-query analysis report.

        An explicit "分析/报告/诊断/复盘" request still runs the report; a
        vague performance question gets a scope clarification with concrete
        next questions instead.
        """
        explicit = (
            "分析", "报告", "经营分析", "诊断", "复盘", "评估",
            "洞察", "趋势分析", "对比分析", "概览", "总结", "拆解",
        )
        if any(marker in text for marker in explicit):
            return False
        subject = any(
            marker in text for marker in ("卖得", "生意", "经营", "业绩", "销售")
        )
        vague = any(
            marker in text for marker in ("怎么样", "如何", "好不好", "什么情况")
        )
        return subject and vague

    def _unsupported_concept(self, text: str) -> dict | None:
        """Explain out-of-scope business concepts without generic word soup.

        Prediction and profit are common questions whose honest answer is
        "the data cannot answer this". Returning a tailored explanation and
        historical alternatives is more useful than "没有识别出...指标".
        """
        value = str(text or "")
        if "为什么" in value and any(
            marker in value for marker in ("不能", "无法", "没有")
        ):
            return None
        concepts: list[set[str]] = []
        for metric in self.sem.metrics.values():
            concepts.append({str(metric.name), *map(str, metric.synonyms)})
        for dim in self.sem.dimensions.values():
            concepts.append({str(dim.raw.get("name_zh") or dim.id)})
        known = " ".join(" ".join(item) for item in concepts)
        lo, hi = self.sem.time_range
        range_text = f"{lo} ~ {hi}" if lo and hi else "当前历史区间"

        forecast = bool(re.search(
            r"(预测|预估|推算).{0,24}(卖|销售|销量|订单|营收|收入|多少)"
            r"|(下个?月|明年|未来).{0,24}(卖|销售|销量|订单|营收|收入)"
            r"|能.{0,8}预测",
            value,
        ))
        if forecast and "预测" not in known:
            return {
                "kind": "forecast",
                "message": (
                    "我不能用这组数据预测下个月能卖多少。\n\n"
                    "这里只有已经发生的订单、支付和评价，没有预测模型、"
                    "促销/季节性假设或外部变量；直接外推一个未来数字并不可靠，"
                    "所以我不编造。\n\n"
                    f"当前数据覆盖历史区间（{range_text}）。"
                    "如果你要做判断，我可以先给你历史基线："
                ),
                "options": [
                    "2025年逐月商品销售额和订单数",
                    "2025年各品类商品销售额",
                    "2025年12月商品销售额环比",
                ],
            }

        profit_terms = ("利润", "毛利", "净利", "利润率", "成本", "采购价", "费用")
        if any(term in value for term in profit_terms) and not any(
            term in known for term in profit_terms
        ):
            return {
                "kind": "profit",
                "message": (
                    "这组数据里没有成本、采购价或费用字段，"
                    "所以无法计算利润或利润率；商品销售额也不等于利润。\n\n"
                    "能查的是规模类指标：商品销售额、运费合计、支付金额、"
                    "订单数、复购客户数。你可以先看这些，但不能把它们当利润。"
                ),
                "options": [
                    "商品销售额是多少？",
                    "运费合计是多少？",
                    "2025年逐月商品销售额是多少？",
                ],
            }
        return None

    _JOIN_EXPLANATION_RE = re.compile(
        r"(?:直接|把|将).{0,24}(?:关联|连接|join|联表).{0,40}"
        r"(?:为什么|导致).{0,24}(?:重复|放大|笛卡尔|一对多|多对多)"
        r"|为什么.{0,32}(?:关联|连接|join).{0,32}(?:重复|放大)",
        re.I,
    )

    def _apply_reference_focus(self, result, context: dict | None,
                               question: str) -> None:
        """Bind “其中/这些/该…” to the active result's entity list.

        The parser may label a result-reference turn as a brand-new query. The
        deterministic focus projection still knows the previous grouped result,
        so use it rather than dropping the dimension and time scope.
        """
        if context is None or result.intent_type not in {"query", "lookup"}:
            return
        focus = context.get("focus") or {}
        dimension = focus.get("entity_dimension")
        values = list(focus.get("values") or [])
        if not dimension or not values:
            return
        if not re.search(r"其中|这些|上述|该|这个|那些", str(question or "")):
            return
        if result.context_action in {None, "new"}:
            result.context_action = "reference_result"
        slots = result.slots
        if not slots.get("dimensions"):
            slots["dimensions"] = [dimension]
        filters = dict(slots.get("filters") or {})
        if dimension not in filters:
            filters[dimension] = {"op": "in", "value": values}
        slots["filters"] = filters
        slots["notes"] = list(dict.fromkeys(
            list(slots.get("notes") or [])
            + [f"引用当前结果中的 {dimension} 成员范围。"]
        ))

    def _apply_sample_threshold(self, slots: dict, question: str) -> None:
        """Keep an explicit per-group sample threshold even if the LLM omits it.

        This is deliberately narrow: it only applies to a grouped ratio query
        with an explicit "至少 N 笔/单/次" phrase. The aggregate is the ratio
        denominator (`order_count`), not a made-up metric.
        """
        if slots.get("having"):
            return
        dims = slots.get("dimensions") or []
        if not dims:
            return
        metric_id = slots.get("metric") or slots.get("ratio_numerator")
        if not metric_id:
            return
        try:
            metric = self.sem.metric(str(metric_id))
        except Exception:
            return
        if metric.type != "ratio":
            return
        match = re.search(
            r"(?:至少|不少于|最少|超过|大于)\s*([0-9一二三四五六七八九十百两]+)\s*(?:笔|单|次)",
            str(question or ""),
        )
        if not match:
            return
        raw = match.group(1)
        numerals = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }
        try:
            value = int(raw) if raw.isdigit() else numerals.get(raw, 0)
        except ValueError:
            value = 0
        if value <= 0:
            return
        slots["having"] = [{
            "metric": "order_count",
            "op": "gte",
            "value": value,
            "required": True,
        }]
        slots["notes"] = list(dict.fromkeys(
            list(slots.get("notes") or [])
            + [f"从问题文本补充样本阈值：已送达订单数 >= {value}。"]
        ))

    def _normalize_entity_ratio_slots(self, slots: dict,
                                      question: str = "") -> None:
        """Bind an entity-count numerator to a same-entity denominator.

        Parsers often choose a generic customer/order count for "占同期...
        比例" even though the numerator is an entity_count metric. That is a
        scope mismatch. For entity ratios the valid denominator is the same
        entity metric with a lower event threshold; do that deterministically
        instead of hardcoding a question or dataset field.
        """
        numerator_id = slots.get("ratio_numerator") or slots.get("metric")
        denominator_id = slots.get("ratio_denominator")
        if not numerator_id:
            return
        try:
            numerator = self.sem.metric(str(numerator_id))
        except Exception:
            return
        if numerator.type != "entity_count":
            return
        if not denominator_id:
            # The parser often treats "占同期自然客户的比例" as a plain
            # entity-count query. If the question explicitly asks for a
            # ratio, bind the denominator to the same entity metric at the
            # base threshold instead of answering the numerator alone.
            if re.search(r"占|占比|比例|份额|比重|share", str(question or "")):
                denominator_id = numerator.id
            else:
                return
        try:
            denominator = self.sem.metric(str(denominator_id))
        except Exception:
            return
        if denominator.type == "entity_count" and str(denominator.id) == str(numerator.id):
            pass
        elif denominator.type == "entity_count":
            # A different entity must be handled by capability validation;
            # do not silently swap it.
            return
        else:
            # Ordinary customer/order counts have a different entity/scope.
            # Use the same entity metric at min_events=1.
            denominator_id = numerator.id
        slots["ratio_numerator"] = numerator.id
        slots["ratio_denominator"] = denominator_id
        if not slots.get("ratio_numerator_min_events"):
            entity = slots.get("entity_count")
            if isinstance(entity, dict) and entity.get("min_events"):
                slots["ratio_numerator_min_events"] = int(entity["min_events"])
            elif isinstance(slots.get("nested"), dict):
                slots["ratio_numerator_min_events"] = int(
                    slots["nested"].get("min_orders") or 1
                )
        slots["ratio_denominator_min_events"] = 1
        slots["notes"] = list(dict.fromkeys(
            list(slots.get("notes") or [])
            + ["实体占比的分母已绑定到同一 entity_count 指标（min_events=1）。"]
        ))

    def _normalize_entity_ratio_ir(self, ir: QueryIR,
                                   question: str = "") -> QueryIR:
        data = ir.as_dict()
        self._normalize_entity_ratio_slots(data, question)
        return replace(
            ir,
            ratio_numerator=data.get("ratio_numerator"),
            ratio_denominator=data.get("ratio_denominator"),
            ratio_numerator_min_events=data.get("ratio_numerator_min_events"),
            ratio_denominator_min_events=data.get("ratio_denominator_min_events"),
            notes=tuple(data.get("notes") or []),
        )

    def _memory_payload(self, state: SessionState, *,
                        allow_llm: bool = True) -> dict | None:
        if self.memory_compressor is None:
            return None
        user = next(
            (m.get("text", "") for m in reversed(state.messages)
             if m.get("role") == "user"),
            "",
        )
        answer = next(
            (m.get("text", "") for m in reversed(state.messages)
             if m.get("role") == "assistant"),
            "",
        )
        if not user or not answer:
            return None
        artifact = state.artifacts.get(state.last_result_id or "") or {}
        plan = state.plan
        if plan is None and artifact.get("plan"):
            try:
                from app.domain.plan import QueryPlan
                plan = QueryPlan.model_validate(artifact["plan"])
            except Exception:
                plan = None
        try:
            payload = self.memory_compressor.compress(
                previous=state.memory,
                question=user,
                answer=answer,
                plan=plan,
                artifact=artifact,
                recent_messages=state.messages[-8:],
                turn=state.turn,
                pending=state.pending,
                allow_llm=allow_llm,
            )
        except Exception as exc:
            log_event(
                logger, "memory.compress_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        if not isinstance(payload, dict) or not payload.get("summary"):
            return None
        payload["turn"] = state.turn
        return payload

    def _schedule_memory_compression(self, session_id: str, state: SessionState,
                                    trace_id: str) -> None:
        """Run semantic compression after the answer without blocking the stream."""
        if self.memory_compressor is None:
            return
        if state.turn < 2 or not getattr(self.memory_compressor, "available", False):
            return
        if not any(m.get("role") == "assistant" for m in state.messages):
            return

        def run() -> None:
            try:
                payload = self._memory_payload(state, allow_llm=True)
                if payload:
                    self.store.append(
                        session_id, "memory.compressed", payload,
                        trace_id=trace_id,
                    )
            except Exception as exc:
                log_event(
                    logger, "memory.background_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

        threading.Thread(target=run, name=f"memory-{session_id}", daemon=True).start()

    def _schema_join_explanation(self, question: str) -> str | None:
        """Answer a schema/join grain question without treating it as a query.

        This is a deterministic explanation path for questions such as
        “直接关联商品明细和支付表为什么会导致金额重复？”. It is not a
        substitute for a data query: it uses table grains and the known join
        graph, and intentionally emits no SQL.
        """
        if not self._JOIN_EXPLANATION_RE.search(question or ""):
            return None
        card = getattr(self.ctx, "model_card", None) or {}
        tables = dict(card.get("tables") or {})
        if not tables:
            tables = {tid: {} for tid in self.sem.tables}

        def find(terms: tuple[str, ...]) -> tuple[str, dict] | None:
            for tid, meta in tables.items():
                hay = " ".join([
                    str(tid),
                    str(meta.get("label") or ""),
                    str(meta.get("description") or ""),
                ]).lower()
                if any(term.lower() in hay for term in terms):
                    return tid, meta
            return None

        item = find(("order_items", "商品明细", "订单明细", "items"))
        payment = find(("order_payments", "支付表", "支付流水", "payments"))
        if not item or not payment:
            # Do not invent order-table names for a different dataset. A
            # generic grain explanation is still useful and cannot mislabel
            # tables as order_items/order_payments.
            return (
                "如果两张表不是同一粒度，直接按业务主键关联会产生笛卡尔组合："
                "一边一个主键有多行，另一边也有多行，关联后的行数是两边行数的乘积。"
                "此时对任一边的金额列求和都会被放大。\n\n"
                "正确做法是先在各自事实表内聚合到共同业务粒度，再通过主表桥接，"
                "不要让两个一对多/多对多事实表直接互连。"
            )
        item_id, item_meta = item
        pay_id, pay_meta = payment
        item_label = str(item_meta.get("label") or item_id)
        pay_label = str(pay_meta.get("label") or pay_id)
        item_grain = "订单 + 明细行"
        pay_grain = "订单 + 支付流水"
        return (
            f"原因在于这两张表不是同一粒度：\n\n"
            f"1. **{item_label}** 的粒度是「{item_grain}」。一笔订单可以有多条商品明细，"
            f"每条明细各有一个 `price`；\n"
            f"2. **{pay_label}** 的粒度是「{pay_grain}」。一笔订单也可以有多条支付流水，"
            f"每条流水各有一个 `payment_value`。\n\n"
            "如果只按 `order_id` 把两张表直接关联，数据库会生成两边行数的组合："
            "一笔订单有 2 条明细和 2 条支付流水时，关联结果就是 2 × 2 = 4 行，"
            "`SUM(price)` 和 `SUM(payment_value)` 都会被放大。\n\n"
            "正确做法是先在各自事实表内按 `order_id` 聚合到订单粒度，"
            "再与 `orders` 关联，或者分别聚合后再用订单表做桥接，"
            "不要让两个一对多事实表直接互连。"
        )

    _EXPLICIT_TIME_RE = re.compile(
        r"\d{4}\s*年|\d{1,2}\s*月|本月|上月|今年|去年|上半年|下半年|近|最近|同比|环比|Q[1-4]",
        re.I,
    )
    _TIME_ANAPHORA_RE = re.compile(r"这个|上述|这些|其中|该|同样|那|再按|前面|刚才|上面")

    def _clear_spurious_time_slots(self, slots: dict, question: str,
                                  conversation_context: dict | None) -> bool:
        if slots.get("time") is None and slots.get("comparison") is None:
            return False
        text = str(question or "")
        if self._EXPLICIT_TIME_RE.search(text) or self._TIME_ANAPHORA_RE.search(text):
            return False
        context = conversation_context or {}
        previous = context.get("active_scope") or context.get("previous_scope") or {}
        previous_metric = previous.get("metric")
        if not slots.get("metric") or not previous_metric:
            return False
        if str(slots.get("metric")) == str(previous_metric):
            return False
        slots["time"] = None
        slots["comparison"] = None
        notes = list(slots.get("notes") or [])
        notes.append("新指标未沿用上一轮时间范围")
        slots["notes"] = list(dict.fromkeys(notes))
        return True

    def _drop_spurious_inherited_time(self, result, question: str,
                                      conversation_context: dict | None) -> None:
        """A new metric without a time word must not inherit the prior month.

        The model sometimes copies the previous scope into a brand-new metric
        question (e.g. Q5 is May delay rate, Q6 asks average rating without
        saying May). ``apply_context_action`` only fills missing fields; it
        cannot undo a time the model already emitted. Correct that here, and
        also clean the alternative interpretations so the answer cannot show
        a stale May interpretation beside the full-period one.
        """
        if result.intent_type not in {"query", "lookup"}:
            return
        self._clear_spurious_time_slots(result.slots, question, conversation_context)
        interps = list(getattr(result, "interpretations", None) or [])
        if not interps:
            return
        cleaned = []
        seen = set()
        for interp in interps:
            slots = dict(interp.get("slots") or {})
            original_time = slots.get("time")
            changed = self._clear_spurious_time_slots(
                slots, question, conversation_context
            )
            key = json.dumps(
                {
                    "metric": slots.get("metric"),
                    "dimensions": slots.get("dimensions"),
                    "filters": slots.get("filters"),
                    "time": slots.get("time"),
                    "comparison": slots.get("comparison"),
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            if key in seen:
                continue
            seen.add(key)
            cleaned.append({
                **interp,
                "slots": slots,
                "_original_time": original_time,
                "_time_was_cleared": changed,
            })
        if len(cleaned) < len(interps):
            # Prefer the interpretation that already represented the full
            # period over the one whose inherited month was cleared.
            preferred = None
            for interp in cleaned:
                if not interp.get("_time_was_cleared"):
                    preferred = interp
                    break
            preferred = preferred or cleaned[0]
            result.interpretations = [{k: v for k, v in i.items()
                                       if not k.startswith("_")}
                                      for i in cleaned]
            result.preferred_interpretation = preferred.get("id")
            result.slots = dict(preferred.get("slots") or {})

    def _handle_parsing(self, state: SessionState) -> list[tuple[str, dict]]:
        original = self._last_user_text(state)
        rw = rewrite(original)
        conversation_context = (
            state.context
            or build_conversation_context(state, self.sem)
        )
        events: list[tuple[str, dict]] = []

        if rw.kind == "smalltalk":
            events.append(("answer.composed", {"text": SMALLTALK_TEXT, "result_id": None}))
            events.append(("result.presented", {"result_id": None}))
            return events

        concept = self._unsupported_concept(original)
        if concept is not None:
            events.append(("intent.classified", {
                "intent_type": "unsupported",
                "tier": "C",
                "confidence": 0.9,
                "rewritten_question": original,
                "context_action": "new",
            }))
            events.append(("answer.composed", {
                "text": concept["message"],
                "result_id": None,
                "options": concept["options"],
            }))
            events.append(("result.presented", {"result_id": None}))
            return events

        join_explanation = self._schema_join_explanation(original)
        if join_explanation:
            events.append(("intent.classified", {
                "intent_type": "explain",
                "tier": "B",
                "confidence": 0.9,
                "rewritten_question": original,
                "context_action": "new",
            }))
            events.append(("answer.composed", {
                "text": join_explanation,
                "result_id": None,
            }))
            events.append(("result.presented", {"result_id": None}))
            return events
        if rw.kind == "lookup" and self.sem.parser == "ecommerce":
            if not self.sem.capabilities.get("member_list", True):
                events.append(("error.raised", {
                    "code": "LOOKUP_UNSUPPORTED",
                    "message": "当前数据源暂不支持维度成员清单查询。" + self._suggestions(),
                    "recoverable": False,
                }))
                return events
            # Enumeration is a plan shape, not a shortcut: emit slots and let
            # the normal state machine ground, plan, compile, check and answer.
            if not rw.lookup_dim:
                events.append(("error.raised", {
                    "code": "LOOKUP_UNSUPPORTED",
                    "message": "没有识别出要列举的维度。" + UNSUPPORTED_SUGGESTIONS,
                    "recoverable": False,
                }))
                return events
            events.append(("intent.parsed", {
                "slots": {"intent": "lookup", "dimensions": [rw.lookup_dim]},
                "notes": ["识别为维度成员枚举（确定性归一化）"],
            }))
            return events

        ctx = project_for_parser(
            state,
            dim_values_path=self.dim_values_path,
            conversation_context=conversation_context,
        )
        try:
            result = self._call_with_retry(
                "parse_intent", lambda: self.parser.parse(rw.normalized, ctx), state, events
            )
        except Exception as exc:
            log_event(logger, "parse.error", error=f"{type(exc).__name__}: {exc}",
                      detail=traceback.format_exc()[:4000])
            events.append(("error.raised", {
                "code": "PARSE_FAILED",
                "message": "暂时无法理解这个问题。可以换一种说法，或指定指标和时间后重试。",
                "recoverable": False,
            }))
            return events

        self._apply_reference_focus(result, conversation_context, original)
        apply_context_action(result, conversation_context, self._last_user_text(state))
        self._drop_spurious_inherited_time(result, original, conversation_context)
        self._normalize_entity_ratio_slots(result.slots, original)
        self._apply_sample_threshold(result.slots, original)
        self._salvage_derived_filter(original, result.slots)
        self._salvage_time_axis(original, result.slots)
        self._salvage_share(original, result.slots)
        self._salvage_ratio_expression(original, result.slots)
        self._drop_implicit_dims_for_comparison(original, result.slots)
        result.unresolved = list(result.slots.get("unresolved") or [])

        events.append(("intent.classified", {
            "intent_type": result.intent_type, "tier": result.tier,
            "confidence": result.confidence,
            "rewritten_question": result.rewritten_question,
            "context_action": result.context_action,
            "context_source": result.context_source,
            "context_confidence": result.context_confidence,
        }))

        if result.intent_type == "smalltalk":
            events.append(("answer.composed", {"text": SMALLTALK_TEXT, "result_id": None}))
            events.append(("result.presented", {"result_id": None}))
            return events

        if result.intent_type == "analysis":
            if self._is_vague_analysis(original):
                events.append(("ambiguity.detected", {
                    "field": "analysis_scope",
                    "term": original,
                    "candidates": [
                        "2025年逐月商品销售额和订单数",
                        "2025年12月商品销售额环比",
                        "2025年各品类商品销售额",
                        "2025年各城市商品销售额",
                    ],
                    "message": (
                        "「卖得怎么样」可以看好几个角度，我先确认一下你想看哪一类：\n"
                        "- 整体趋势（月度销售额/订单数）\n"
                        "- 最近一个月（环比）\n"
                        "- 品类或城市结构\n"
                        "- 配送/口碑等体验指标\n\n"
                        "你选一个，我直接查具体数据，不生成整份经营报告。"
                    ),
                    "kind": "scope",
                    "partial_slots": result.slots,
                }))
                return events
            return events + self._analysis_events(state, None)

        if result.intent_type == "critique_response":
            return events + self._critique_response_events(state, result)

        if result.intent_type == "coverage":
            slots = dict(result.slots)
            slots["intent"] = "coverage"
            slots.setdefault("coverage_period", "month")
            events.append(("intent.parsed", {
                "slots": slots,
                "notes": result.notes,
            }))
            return events

        if len(result.subquestions) >= 2 and not result.interpretations:
            events.append(("subquestions.planned", {
                "questions": [q.get("question", "") for q in result.subquestions],
                "items": [
                    {
                        "id": f"sq{idx}",
                        "question": q.get("question", ""),
                        "intent_type": q.get("intent_type", "query"),
                        "context_action": q.get("context_action", "new"),
                        "error": q.get("error"),
                    }
                    for idx, q in enumerate(result.subquestions, 1)
                ],
                "depth": 1,
            }))
            return events + self._multi_question_events(state, result)

        if result.intent_type == "lookup":
            if not self.sem.capabilities.get("member_list", True):
                events.append(("error.raised", {
                    "code": "LOOKUP_UNSUPPORTED",
                    "message": "当前数据源暂不支持维度成员清单查询。" + self._suggestions(),
                    "recoverable": False,
                }))
                return events
            dim = result.lookup_dim or rw.lookup_dim
            if not dim:
                events.append(("error.raised", {
                    "code": "LOOKUP_UNSUPPORTED",
                    "message": "没有识别出要列举的维度。" + UNSUPPORTED_SUGGESTIONS,
                    "recoverable": False,
                }))
                return events
            slots = dict(result.slots)
            slots["intent"] = "lookup"
            slots["dimensions"] = [dim]
            # Keep filters/time from the parse result: they are the scope of
            # the list and must reach grounding and SQL.
            events.append(("intent.parsed", {"slots": slots, "notes": result.notes}))
            return events

        if result.intent_type == "explain":
            events.append(("answer.composed", {
                "text": result.explanation or "这是一条口径解释。", "result_id": None,
            }))
            events.append(("result.presented", {"result_id": None}))
            return events

        if result.intent_type == "unsupported":
            partial = result.slots or {}
            if partial:
                try:
                    partial_slots = Slots.model_validate(
                        self._normalize_slots_payload(dict(partial))
                    )
                except Exception:
                    partial_slots = None
                if partial_slots is not None and (
                    partial_slots.metric or partial_slots.dimensions
                ):
                    report = assess_slots(
                        partial_slots, self.sem, policy=self.policy,
                        model=getattr(self.ctx, "model", None),
                    )
                    if report.refused:
                        events.append(("capability.assessed", report.as_dict()))
                        events.append(("refusal.decided", report.refusal_payload()))
                        events.append(("answer.composed", {
                            "text": render_refusal(report, self.sem),
                            "result_id": None,
                            "status": "refused",
                        }))
                        events.append(("result.presented", {"result_id": None}))
                        return events
            message = result.explanation or "这个问题暂时不支持。"
            metrics = [
                self.sem.metric(mid).name for mid in list(self.sem.metrics)[:6]
            ]
            dims = [
                str(d.raw.get("name_zh") or did)
                for did, d in list(self.sem.dimensions.items())[:6]
            ]
            extra = []
            if metrics:
                extra.append("可用指标：" + "、".join(metrics))
            if dims:
                extra.append("可用维度：" + "、".join(dims))
            lo, hi = self.sem.time_range
            if lo and hi:
                extra.append(f"数据时间范围：{lo} ~ {hi}")
            if extra:
                message = f"{message}\n\n" + "\n".join(extra)
            repair = build_repair(self.sem, self._repair_slots(state, result.slots))
            if repair["options"]:
                message += (
                    "\n\n可以这样问：\n"
                    + "\n".join(f"- {q}" for q in repair["options"][:3])
                )
            events.append(("answer.composed", {
                "text": message, "result_id": None, "options": repair["options"],
            }))
            events.append(("result.presented", {"result_id": None}))
            return events

        if result.unresolved:
            repair = build_repair(
                self.sem, self._repair_slots(state, result.slots), unknown=result.unresolved,
                model_card=getattr(self.ctx, "model_card", None),
            )
            message = (
                f"「{result.unresolved[0]}」不是当前数据源的指标或维度。\n\n"
                f"{repair_text(repair)}"
            )
            events.append(("ambiguity.detected", {
                "field": "unknown", "term": result.unresolved[0],
                "candidates": repair["options"],
                "message": message,
                "kind": "unresolved",
                "partial_slots": result.slots,
            }))
            return events

        slots = dict(result.slots)
        if result.visualization:
            slots["visualization"] = result.visualization
        if result.subquestions and not result.interpretations:
            first = result.subquestions[0]
            if first.get("slots"):
                slots = dict(first["slots"])
                if result.visualization:
                    slots["visualization"] = result.visualization

        if len(result.interpretations) > 1:
            preferred = result.preferred_interpretation
            meta = [
                {
                    "id": i["id"], "label": i.get("label"),
                    "confidence": i.get("confidence"),
                    "preferred": i["id"] == preferred,
                }
                for i in result.interpretations
            ]
            events.append(("intent.parsed", {
                "slots": slots,
                "ir": QueryIR.from_slots(slots).as_dict(),
                "notes": result.notes,
                "interpretations": meta,
                "preferred_interpretation": preferred,
                "ambiguity_note": result.ambiguity_note,
            }))
            return events + self._multi_interpretation_events(state, result)

        try:
            ir = QueryIR.from_slots(slots)
            events.append(("query.ir.created", ir.as_dict()))
            events.append(("intent.parsed", {
                "slots": slots, "notes": result.notes, "ir": ir.as_dict(),
            }))
        except Exception:
            events.append(("intent.parsed", {"slots": slots, "notes": result.notes}))
        return events

    def _analysis_request_text(self, options: dict | None) -> str:
        options = options or {}
        catalog = {item["id"]: item["label"] for item in module_catalog(self.sem)}
        sections = options.get("sections") or []
        labels = [catalog.get(section, section) for section in sections]
        text = "生成经营分析"
        if labels:
            text += "：" + "、".join(labels)
        custom = str(options.get("custom_text") or "").strip()
        if custom:
            text += "\n自定义分析：" + custom[:500]
        return text

    def _coverage_answer(self, state: SessionState, artifact: dict | None = None) -> str:
        artifact = artifact or state.artifacts.get(state.last_result_id or "") or {}
        facts = artifact.get("facts") or state.facts or {}
        expected = facts.get("expected_periods") or []
        observed = facts.get("observed_periods") or []
        missing = facts.get("missing_periods") or []
        zero_value = facts.get("zero_value_periods") or []
        if not expected:
            return "当前没有可用的月份覆盖结果，无法判断数据是否有缺失。"
        scope = ""
        plan_payload = artifact.get("plan") or {}
        filters = plan_payload.get("filters") or {}
        if filters:
            labels = []
            for dim, spec in filters.items():
                name = self.sem.dimension(dim).raw.get("name_zh", dim)
                value = spec.get("value") if isinstance(spec, dict) else spec
                labels.append(f"{name}={value}")
            scope = f"（{'、'.join(labels)}）"
        lines = [
            f"此次按自然月检查{scope}，应覆盖 {len(expected)} 个月，"
            f"实际有记录 {len(observed)} 个月，覆盖率 "
            f"{float(facts.get('coverage_ratio', 0)) * 100:.2f}%。"
        ]
        if missing:
            lines.append("存在缺失记录的时间段：" + "、".join(missing) + "。")
            internal = [
                month for month in missing
                if expected.index(month) not in {0, len(expected) - 1}
            ]
            boundary = [month for month in missing if month not in internal]
            if boundary:
                lines.append(
                    "缺失分布："
                    + "、".join(boundary)
                    + " 位于观察窗口边缘，可能来自商品生命周期或数据集起止边界，"
                    "仅凭当前数据不能认定为上游漏数。"
                )
            if internal:
                lines.append(
                    "缺失分布："
                    + "、".join(internal)
                    + " 位于观察窗口内部，更可能是业务断档、商品下架或源数据漏采。"
                )
            else:
                lines.append(
                    "观察窗口内部没有发现月中断档；缺失主要集中在首尾边界。"
                )
        else:
            lines.append("所有预期月份都有记录，未发现时间缺口。")
        if zero_value:
            lines.append(
                "以下月份虽然有记录，但指标值为 0：" + "、".join(zero_value) + "。"
            )
        lines.append(
            "说明：“无记录”代表该月份没有落入当前商品和筛选范围的记录；"
            "“零值”代表有记录但业务指标为 0，两者含义不同。"
        )
        coverage_rows = facts.get("coverage_rows") or []
        observed_counts = [
            row.get("record_count") for row in coverage_rows
            if not row.get("is_missing") and row.get("record_count") is not None
        ]
        if observed_counts:
            low = min(observed_counts)
            high = max(observed_counts)
            low_months = [
                row["month"] for row in coverage_rows
                if row.get("record_count") == low and not row.get("is_missing")
            ]
            high_months = [
                row["month"] for row in coverage_rows
                if row.get("record_count") == high and not row.get("is_missing")
            ]
            lines.append(
                f"有记录月份的记录数范围是 {low} 至 {high}；"
                f"最低出现在 {'、'.join(low_months)}，最高出现在 {'、'.join(high_months)}。"
            )
        if missing:
            lines.append(
                "对结论的影响：全期累计值可以使用，但月度趋势、连续增长和同期比较"
                "必须把缺失月份单独说明，不能把“无记录”等同于“业务量为 0”。"
            )
        quality_notes = [
            issue for issue in (artifact.get("checks") or state.checks)
            if issue.get("id") == "DATA_QUALITY"
        ]
        if quality_notes:
            lines.append("数据提示：" + (
                quality_notes[0].get("detail") or quality_notes[0].get("title", "")
            ))
        return "\n\n".join(lines)

    def _critique_response_events(self, state: SessionState,
                                  result) -> list[tuple[str, dict]]:
        """Treat feedback on the previous answer as a refinement, not a new job."""
        artifact = state.artifacts.get(state.last_result_id or "") or {}
        active_plan = artifact.get("plan") or {}
        if active_plan.get("kind") == "coverage":
            text = self._coverage_answer(state, artifact)
            return [
                ("answer.composed", {
                    "text": text,
                    "result_id": state.last_result_id,
                    "kind": "critique_response",
                }),
                ("result.presented", {"result_id": state.last_result_id}),
            ]
        try:
            plan = QueryPlan.model_validate(active_plan)
        except Exception:
            return [("answer.composed", {
                "text": result.explanation
                or "你说得对。当前没有可用于继续检查的查询结果，请指定要检查的实体和指标。",
                "result_id": None,
            })]
        slots = Slots(
            metric=plan.metric,
            dimensions=[],
            time=plan.time,
            filters=plan.filters,
            intent="coverage",
            notes=[
                "根据用户对上一轮答案的反馈，继续检查上一查询范围的时间覆盖。",
            ],
        )
        return [("intent.parsed", {
            "slots": slots.model_dump(mode="json"),
            "notes": slots.notes,
        })]

    def _analysis_focus(self, state: SessionState) -> dict:
        """Carry only explicit, bindable context from the latest query."""
        for artifact in reversed(list(state.artifacts.values())):
            plan_payload = (artifact or {}).get("plan")
            if not plan_payload:
                continue
            try:
                plan = QueryPlan.model_validate(plan_payload)
            except Exception:
                continue
            filters = {
                dim: spec.model_dump(mode="json")
                for dim, spec in plan.filters.items()
            }
            anchor_end = None
            if plan.comparison:
                anchor_end = str(plan.comparison.current.end)
            elif plan.time:
                anchor_end = str(plan.time.end)
            labels = []
            for dim, spec in filters.items():
                name = self.sem.dimension(dim).raw.get("name_zh", dim)
                value = spec.get("value")
                if isinstance(value, list):
                    value = "/".join(map(str, value))
                labels.append(f"{name}={value}")
            return {
                "filters": filters,
                "anchor_end": anchor_end,
                "focus_label": "、".join(labels),
            }
        return {}

    def _unsupported_period_text(self, question: str) -> str | None:
        """Return a business-facing message when the question's year is absent."""
        years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", question or "")
        if not years:
            return None
        lo = str(self.sem.time_range[0])[:4]
        hi = str(self.sem.time_range[1])[:4]
        outside = sorted({year for year in years if year < lo or year > hi})
        if not outside:
            return None
        return (
            f"当前数据只覆盖 {self.sem.time_range[0]} 至 {self.sem.time_range[1]}，"
            f"问题涉及的 {'、'.join(outside)} 不在数据范围内，无法做同比。"
            "可以改为当前覆盖期内的月度趋势或环比。"
        )

    @staticmethod
    def _dependency_anchor(question: str, anchors: list[dict],
                           action: str) -> dict | None:
        """Find the previous result a follow-up refers to."""
        if not anchors:
            return None
        # Root parses often label sibling follow-ups as "new" even when the
        # wording points at the previous result ("该品类", "这个城市").
        referential = bool(re.search(r"该|这个|这些|那个|上述|上一|前一个", question or ""))
        if action not in {"inherit", "refine", "reference_result"} and not referential:
            return None
        for anchor in reversed(anchors):
            name = str(anchor.get("name") or "")
            if name and name in question:
                return anchor
        if referential and len(anchors) == 1:
            return anchors[-1]
        return None

    def _register_anchor(self, entry: dict, anchors: list[dict]) -> None:
        """Expose a single-dimension result as an anchor for later subquestions."""
        plan = entry.get("plan")
        artifact = entry.get("artifact")
        dimensions = list(getattr(plan, "dimensions", None) or [])
        rows = list(getattr(artifact, "rows", None) or [])
        if len(dimensions) != 1 or not rows:
            return
        dimension = dimensions[0]
        value = rows[0][0]
        if value is None:
            return
        name = dimension
        item = self.sem.dimensions.get(dimension)
        if item is not None:
            name = str(item.raw.get("name_zh") or dimension)
        anchors.append({
            "dimension": dimension,
            "name": name,
            "value": str(value),
        })

    def _parse_subquestion_locally(self, state: SessionState, sub: dict,
                                   question: str, anchors: list[dict],
                                   events: list) -> tuple[dict | None, dict | None]:
        """Re-parse one subquestion with its own brief and dependency hint.

        The root parse is intentionally only a decomposer. This second pass is
        the local semantic grounding: the brief is built from this subquestion,
        not from the whole message, and it can carry the top-dimension anchor
        from an already executed sibling.
        """
        action = str(sub.get("context_action") or "new")
        anchor = self._dependency_anchor(question, anchors, action)
        parse_text = question
        if anchor:
            parse_text = (
                f"{question}\n\n"
                f"[上一子问题已确定 {anchor['name']} = {anchor['value']}。"
                "如果本问题使用“该/这个/上述”等指代，必须把这个值保留为筛选条件。]"
            )
        context = project_for_parser(
            state,
            dim_values_path=self.dim_values_path,
        )
        try:
            parsed = self._call_with_retry(
                "parse_subquestion",
                lambda: self.parser.parse(parse_text, context),
                state,
                events,
            )
        except Exception:
            return None, anchor
        slots = dict(getattr(parsed, "slots", None) or {})
        if not slots.get("metric") and not slots.get("metric_spec"):
            return None, anchor
        self._normalize_entity_ratio_slots(slots, question)
        self._apply_sample_threshold(slots, question)
        return slots, anchor

    def _inject_anchor_filter(self, slots_payload: dict, question: str,
                              anchor: dict | None, action: str) -> None:
        if not anchor:
            return
        filters = dict(slots_payload.get("filters") or {})
        dimension = anchor["dimension"]
        existing = filters.get(dimension) if dimension else None
        existing_value = existing.get("value") if isinstance(existing, dict) else existing
        placeholder = any(
            token in str(existing_value or "")
            for token in ("最高", "最低", "该", "这个", "那个", "上述", "上一年")
        )
        if dimension and (existing is None or placeholder):
            filters[dimension] = {"op": "eq", "value": anchor["value"]}
            slots_payload["filters"] = filters
        # A local parse may mark "该品类" unresolved before it sees the anchor.
        # Once the anchor is bound, that term is no longer unresolved.
        unresolved = []
        for term in slots_payload.get("unresolved") or []:
            text = str(term)
            if anchor["name"] in text or "该品类" in text:
                continue
            unresolved.append(term)
        slots_payload["unresolved"] = unresolved

    def _normalize_subquestion_scope(self, slots_payload: dict) -> None:
        """Do not let a ranking limit truncate a trend or a full share result."""
        dims = list(slots_payload.get("dimensions") or [])
        try:
            limit = int(slots_payload.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        time_dims = []
        for dim in dims:
            if dim == "month":
                time_dims.append(dim)
                continue
            item = self.sem.dimensions.get(dim)
            if item is not None and str(item.raw.get("kind") or "") == "time":
                time_dims.append(dim)
        if time_dims and limit < 100:
            slots_payload["limit"] = 100
        elif slots_payload.get("share") and dims and not slots_payload.get("limit_per_group") and limit <= 1:
            slots_payload["limit"] = 100

    def _multi_question_events(self, state: SessionState,
                               result) -> list[tuple[str, dict]]:
        """Execute sub-questions, then compose all outcomes together."""
        events: list[tuple[str, dict]] = []
        presentation_items: list[dict] = []
        executed: list[dict] = []
        non_sql: list[dict] = []
        failures: list[dict] = []
        previous_slots: dict | None = None
        anchors: list[dict] = []
        questions = list(result.subquestions[:MAX_SUBQUESTIONS])

        for index, sub in enumerate(questions, 1):
            sid = f"sq{index}"
            question = str(sub.get("question") or f"第 {index} 个问题").strip()
            intent_type = str(sub.get("intent_type") or "query")
            events.append(("subquestion.started", {
                "subquestion_id": sid,
                "index": index,
                "question": question,
                "intent_type": intent_type,
            }))

            if intent_type == "explain":
                text = sub.get("explanation") or "这是一个口径解释问题。"
                non_sql.append({"id": sid, "question": question, "text": text})
                presentation_items.append({
                    "id": sid,
                    "index": index,
                    "question": question,
                    "status": "explain",
                    "text": text,
                })
                continue
            if intent_type in {"unsupported", "smalltalk", "analysis"}:
                text = sub.get("explanation") or "这个问题暂时无法生成可执行查询。"
                non_sql.append({"id": sid, "question": question, "text": text})
                presentation_items.append({
                    "id": sid,
                    "index": index,
                    "question": question,
                    "status": "unsupported",
                    "text": text,
                })
                continue

            unsupported_period = self._unsupported_period_text(question)
            if unsupported_period:
                reason = "OUT_OF_RANGE_PERIOD"
                failures.append({"id": sid, "question": question, "reason": reason})
                events.append(("subquestion.rejected", {
                    "subquestion_id": sid,
                    "status": "unsupported",
                    "reason": unsupported_period,
                }))
                presentation_items.append({
                    "id": sid,
                    "index": index,
                    "question": question,
                    "status": "unsupported",
                    "text": unsupported_period,
                })
                continue

            action = str(sub.get("context_action") or "new")
            root_slots = dict(sub.get("slots") or {})
            local_slots, anchor = self._parse_subquestion_locally(
                state, sub, question, anchors, events,
            )
            slots_payload = local_slots or root_slots

            if sub.get("error") and not local_slots:
                reason = f"子问题槽位非法：{sub['error']}"
                failures.append({"id": sid, "question": question, "reason": reason})
                events.append(("subquestion.rejected", {
                    "subquestion_id": sid,
                    "status": "failed",
                    "reason": reason,
                }))
                presentation_items.append({
                    "id": sid,
                    "index": index,
                    "question": question,
                    "status": "failed",
                    "text": self._safe_subquestion_failure(reason),
                })
                continue

            if intent_type == "lookup":
                dim = sub.get("lookup_dimension") or sub.get("list_dim")
                slots_payload["intent"] = "lookup"
                slots_payload["dimensions"] = [dim] if dim else []
            if previous_slots and action in {"inherit", "refine"} and not local_slots:
                inherited = dict(previous_slots)
                inherited.update({
                    key: value
                    for key, value in slots_payload.items()
                    if value not in (None, [], {})
                })
                inherited["filters"] = {
                    **(previous_slots.get("filters") or {}),
                    **(slots_payload.get("filters") or {}),
                }
                slots_payload = inherited
            self._inject_anchor_filter(slots_payload, question, anchor, action)
            self._normalize_subquestion_scope(slots_payload)

            try:
                sub_events, entry = self._run_subquestion(
                    state, sid, question, slots_payload,
                    preferred=not executed,
                )
            except CapabilityRefusal as exc:
                payload = exc.report.refusal_payload()
                failures.append({
                    "id": sid, "question": question,
                    "reason": payload["code"], "capability": payload,
                })
                events.append(("refusal.decided", {
                    **payload, "subquestion_id": sid,
                }))
                events.append(("subquestion.rejected", {
                    "subquestion_id": sid,
                    "status": "refused",
                    "reason": payload["code"],
                    "capability": payload,
                }))
                presentation_items.append({
                    "id": sid,
                    "index": index,
                    "question": question,
                    "status": "unsupported",
                    "text": render_refusal(exc.report, self.sem),
                })
                continue
            except Exception as exc:
                reason = str(exc)
                failures.append({"id": sid, "question": question, "reason": reason})
                events.append(("subquestion.rejected", {
                    "subquestion_id": sid,
                    "status": "failed",
                    "reason": reason,
                }))
                presentation_items.append({
                    "id": sid,
                    "index": index,
                    "question": question,
                    "status": "failed",
                    "text": self._safe_subquestion_failure(reason),
                })
                continue

            events.extend(sub_events)
            previous_slots = dict(slots_payload)
            executed.append(entry)
            self._register_anchor(entry, anchors)
            context = self._entry_context(entry)
            context["insights"] = build_insights(
                entry["plan"],
                context["columns"],
                context["rows"],
                context["row_count"],
                context["truncated"],
                context["checks"],
                self.sem,
                max_items=5,
            )
            context.update(summarize_rows(
                context.get("rows") or [],
                context.get("columns") or [],
                entry["plan"].dimensions,
                50,
            ))
            presentation_items.append({
                "id": sid,
                "index": index,
                "question": question,
                "status": "query",
                "context": context,
            })

        if not executed and not non_sql:
            return events + [("error.raised", {
                "code": "NO_SUBQUESTION",
                "message": "没有可执行的子问题。请把问题拆成更明确的指标或时间。",
                "recoverable": False,
            })]

        context = {
            "kind": "subquestions",
            "question_count": len(questions),
            "subquestions": presentation_items,
            "memory": state.memory or {},
        }
        try:
            text = self._call_with_retry(
                "compose_subquestions",
                lambda: self.composer.compose(context),
                state,
                events,
            )
        except Exception as exc:
            log_event(
                logger,
                "subquestion.composer.fallback",
                error=f"{type(exc).__name__}: {exc}",
            )
            from app.llm.compose_answer import TemplateComposer
            text = TemplateComposer().compose(context)
        text = self._with_draft_notice(text)
        result_id = executed[0]["artifact"].result_id if executed else None
        events.append(("answer.composed", {
            "text": text,
            "result_id": result_id,
            "kind": "subquestions",
            "semantic_status": self._semantic_status(),
        }))
        events.append(("subquestions.completed", {
            "primary_slots": executed[0]["slots"].model_dump(mode="json")
            if executed else None,
            "results": [
                {
                    "id": entry["id"],
                    "question": entry["question"],
                    "result_id": entry["artifact"].result_id,
                }
                for entry in executed
            ],
            "failures": failures,
        }))
        events.append(("result.presented", {"result_id": result_id}))
        return events

    @staticmethod
    def _safe_subquestion_failure(reason: str) -> str:
        """Translate internal failures into bounded user-facing guidance."""
        if "成员清单" in reason and "时间" in reason:
            return (
                "当前数据源没有可为成员清单应用的时间字段，"
                "因此只能在不限时间或改用时间范围内聚合指标的方式回答。"
            )
        if "没有识别出" in reason or "槽位非法" in reason:
            return (
                "这个子问题没有形成可验证的指标或维度。"
                "可以补充明确的指标名称、时间范围和统计维度后重试。"
            )
        if "查询执行失败" in reason:
            return (
                "这个子问题的查询没有成功执行。"
                "可以缩小时间或维度范围后重试。"
            )
        return (
            "这个子问题暂时无法完成。系统已记录本次问题；"
            "可以补充明确的指标、时间和统计维度后重试。"
        )

    def _run_subquestion(self, state: SessionState, ident: str, question: str,
                         slots_payload: dict, *, preferred: bool,
                         analysis_section: str | None = None,
                         analysis_item: str | None = None
                         ) -> tuple[list[tuple[str, dict]], dict]:
        events: list[tuple[str, dict]] = []
        slots_payload = self._normalize_slots_payload(slots_payload)
        self._salvage_derived_filter(question, slots_payload)
        self._salvage_time_axis(question, slots_payload)
        self._salvage_share(question, slots_payload)
        self._salvage_ratio_expression(question, slots_payload)
        self._drop_implicit_dims_for_comparison(question, slots_payload)
        try:
            slots = Slots.model_validate(slots_payload)
        except Exception as exc:
            raise SemanticError(f"槽位非法：{exc}") from exc
        if slots.unresolved:
            raise SemanticError("没有识别出「" + "、".join(slots.unresolved) + "」")

        parser_context = project_for_parser(
            state, dim_values_path=self.dim_values_path
        )
        ground_args = {
            "filters": slots_payload.get("filters") or {},
            "unresolved": list(slots.unresolved),
        }
        grounded = self.grounder.ground(
            slots.model_dump(mode="json"), slots.unresolved, parser_context
        )
        events.extend(self._tool_events(
            "members.ground",
            ground_args,
            ToolResult(
                "members.ground",
                not bool(grounded.ambiguity),
                {
                    "bindings": grounded.bindings,
                    "notes": grounded.notes,
                    "ambiguity": grounded.ambiguity,
                },
                error_code="AMBIGUOUS_MEMBER" if grounded.ambiguity else None,
                message=(
                    grounded.ambiguity.get("message", "")
                    if grounded.ambiguity else ""
                ),
                repairable=bool(grounded.ambiguity),
            ),
        ))
        if grounded.ambiguity:
            raise SemanticError(grounded.ambiguity.get("message") or "值绑定失败")
        bindings = []
        for binding in grounded.bindings:
            ids = binding.get("ids") or binding.get("values") or []
            if not binding.get("dim") or not ids:
                continue
            slots.filters[binding["dim"]] = FilterSpec(
                op=binding.get("op", "in"), value=list(ids)
            )
            bindings.append(binding)
        events.append(("values.grounded", {
            "bindings": bindings,
            "notes": grounded.notes,
        }))

        self._infer_limit_per_group(question, slots)
        report = assess_slots(
            slots, self.sem, policy=self.policy,
            model=getattr(self.ctx, "model", None),
        )
        events.append(("capability.assessed", {
            **report.as_dict(), "subquestion_id": ident,
        }))
        if report.refused:
            raise CapabilityRefusal(report)

        try:
            if slots.intent == "lookup":
                dim = slots.dimensions[0] if slots.dimensions else None
                plan = member_list_plan(dim, slots, self.sem)
            else:
                slots = apply_degradations(slots, report)
                plan = plan_from_ir(QueryIR.from_slots(slots), self.sem)
                if report.degradations:
                    reasons = [
                        str(d.get("reason")) for d in report.degradations
                        if d.get("reason")
                    ]
                    plan.degraded_reason = "；".join(dict.fromkeys(reasons)) or None
                    plan.degraded_kind = self._degraded_kind(report.degradations)
            plan.interpretation_id = ident
            plan.interpretation_label = question[:32]
            plan.preferred = preferred
        except CapabilityRefusal:
            raise
        except Exception as exc:
            raise SemanticError(str(exc)) from exc
        events.append(("plan.compiled", {"plan": plan.model_dump(mode="json")}))

        try:
            compiled, issues = prepare(plan, self.sem)
        except PipelineError as exc:
            degraded = (
                degrade_unsupported_dimensions(plan, self.sem)
                if any(issue.code == "UNSUPPORTED_GRAIN" for issue in exc.issues)
                else None
            )
            if not degraded:
                raise
            plan, removed = degraded
            plan.interpretation_id = ident
            plan.interpretation_label = question[:32]
            plan.preferred = preferred
            events.append(("plan.degraded", {
                "reason": plan.degraded_reason,
                "removed_dimensions": removed,
                "interpretation_id": ident,
            }))
            events.append(("plan.compiled", {"plan": plan.model_dump(mode="json")}))
            compiled, issues = prepare(plan, self.sem)

        compiler_version = int(self.sem.raw.get("compiler_version", 1))
        fingerprint = compiled.fingerprint(compiler_version, self.sem.version)
        events.append(("sql.compiled", {
            "sql": compiled.sql,
            "params": [str(param) for param in compiled.params],
            "fingerprint": fingerprint,
            "columns": compiled.columns,
            "tables_used": compiled.tables_used,
            "meta": compiled.meta,
            "issues": [issue.as_dict() for issue in issues if issue.severity != "error"],
            "interpretation_id": ident,
            "preferred": preferred,
        }))
        try:
            artifact = execute_prepared(compiled, db_path=self.warehouse)
        except Exception as exc:
            raise SemanticError("查询执行失败，可缩小时间或维度范围后重试。") from exc
        artifact.fingerprint = fingerprint
        artifact.result_id = f"r{state.turn}-{ident}-{fingerprint}"
        payload = artifact.as_dict()
        display = display_spec(plan, self.sem, artifact)
        payload.update({
            "question": question,
            "display": display,
            "chart": recommend_chart(plan, self.sem, display, artifact),
            "plan": plan.model_dump(mode="json"),
            "interpretation_id": ident,
            "interpretation_label": question[:32],
            "preferred": preferred,
            "subquestion_id": ident,
        })
        if analysis_section:
            payload["analysis_section"] = analysis_section
        if analysis_item:
            payload["analysis_item"] = analysis_item
        payload["field_map"] = self._field_map(plan)
        events.append(("sql.executed", payload))

        issues_checked, facts = check(
            plan, self.sem, artifact,
            db_path=self.warehouse, quality=self.quality,
        )
        events.extend(self._enrich_count_members(plan, artifact, facts))
        issue_payload = [issue.as_dict() for issue in issues_checked]
        events.append(("checks.completed", {
            "result_id": artifact.result_id,
            "issues": issue_payload,
            "facts": facts,
            "blocked": any(issue.severity == "error" for issue in issues_checked),
            "interpretation_id": ident,
            "preferred": preferred,
        }))
        return events, {
            "id": ident,
            "question": question,
            "slots": slots,
            "plan": plan,
            "artifact": artifact,
            "issues": issue_payload,
            "facts": facts,
        }

    def _metric_for_plan(self, plan):
        if plan is None:
            return None
        if getattr(plan, "metric_spec", None):
            from app.domain.semantic.loader import Metric

            return Metric(plan.metric, plan.metric_spec)
        if plan.metric and plan.metric in self.sem.metrics:
            return self.sem.metrics[plan.metric]
        return None

    def _entry_context(self, entry: dict) -> dict:
        plan, artifact = entry["plan"], entry["artifact"]
        metric = self._metric_for_plan(plan)
        comparison = (
            plan.comparison.model_dump(mode="json")
            if plan.comparison else None
        )
        time = (
            comparison.get("current") if comparison
            else plan.time.model_dump(mode="json") if plan.time else None
        )
        return {
            "question": entry["question"],
            "metric": plan.metric,
            "metric_name": metric.name if metric else "结果",
            "metric_unit": metric.raw.get("unit") if metric else None,
            "metric_type": metric.type if metric else "aggregate",
            "time_axis": metric.raw.get("time_axis") if metric else None,
            "dimension_labels": [
                self.sem.dimension(dim).raw.get("name_zh", dim)
                for dim in plan.dimensions
            ],
            "rows": artifact.rows,
            "columns": artifact.columns,
            "row_count": artifact.row_count,
            "truncated": artifact.truncated,
            "limit": plan.limit,
            "limit_per_group": plan.limit_per_group,
            "coverage_note": (
                "当前结果是 TopN/每组前 N 行，不是完整总体；"
                "不要用这些行计算总体占比、总量或贡献率。"
                if (plan.limit_per_group or (plan.limit and plan.limit < 100))
                else None
            ),
            "time": time,
            "comparison": comparison,
            "compare": bool(comparison or plan.compare),
            "filters_label": self._plan_filters_label(plan),
            "checks": self._humanize_checks(entry["issues"], self._field_map(plan)),
            "facts": entry["facts"],
            "meta": artifact.meta,
            "field_map": self._field_map(plan),
        }

    def _analysis_events(self, state: SessionState,
                         options: dict | None) -> list[tuple[str, dict]]:
        options = options or {}
        focus = self._analysis_focus(state)
        raw_sections = options.get("sections")
        selected = None if not raw_sections else list(raw_sections)
        custom_text = str(options.get("custom_text") or "").strip()[:500]
        custom_title = str(options.get("custom_title") or "").strip()[:60]
        custom_failure = None
        custom_items: list = []
        custom_scope = None

        if custom_text:
            try:
                parsed = self.parser.parse(
                    custom_text,
                    project_for_parser(
                        state,
                        dim_values_path=self.dim_values_path,
                        conversation_context=build_conversation_context(
                            state, self.sem
                        ),
                    ),
                )
                custom_subs = parsed.subquestions[:MAX_SUBQUESTIONS]
                if custom_subs:
                    for idx, sub in enumerate(custom_subs, 1):
                        if sub.get("slots"):
                            custom_items.append(custom_analysis_item(
                                dict(sub["slots"]),
                                str(sub.get("question") or custom_title or "自定义分析"),
                                f"cx{idx}",
                            ))
                elif (
                    parsed.intent_type == "query"
                    and (
                        parsed.slots.get("metric")
                        or parsed.slots.get("dimensions")
                    )
                ):
                    custom_items.append(custom_analysis_item(
                        dict(parsed.slots), custom_title or "自定义分析", "cx1"
                    ))
                else:
                    parsed_filters = parsed.slots.get("filters") or {}
                    resolved = None
                    if not parsed_filters:
                        resolved = self.grounder.resolve_term(custom_text)
                    if resolved and resolved.get("status") == "resolved":
                        dim = resolved["dimension"]
                        parsed_filters = {
                            dim: {
                                "op": "eq" if len(resolved.get("ids") or []) == 1 else "in",
                                "value": (
                                    resolved["ids"][0]
                                    if len(resolved.get("ids") or []) == 1
                                    else resolved["ids"]
                                ),
                            }
                        }
                    if parsed_filters:
                        focus = dict(focus)
                        filters = dict(focus.get("filters") or {})
                        filters.update(parsed_filters)
                        focus["filters"] = filters
                        custom_scope = {
                            "filters": parsed_filters,
                            "label": custom_text,
                        }
                        focus["custom_scope"] = custom_scope
                        focus["focus_label"] = "、".join(filter(None, [
                            focus.get("focus_label"),
                            custom_text,
                        ]))
                    elif resolved and resolved.get("status") == "ambiguous":
                        custom_failure = "自定义实体存在多个可能维度，请明确业务维度。"
                    else:
                        custom_failure = (
                            parsed.explanation
                            or "自定义分析没有识别出可执行的指标、维度或实体。"
                        )
            except Exception as exc:
                custom_failure = f"自定义分析解析失败：{type(exc).__name__}: {exc}"

        items, window = build_analysis_items(self.sem, focus, selected)
        items.extend(custom_items)
        if custom_failure:
            items.append(custom_analysis_item({}, custom_title or "自定义分析", "cx1"))

        if not items:
            return [("error.raised", {
                "code": "NO_ANALYSIS_MODULE",
                "message": "当前数据源没有足够的已发布指标和维度来生成经营分析。",
                "recoverable": False,
            })]

        events: list[tuple[str, dict]] = [("analysis.requested", {
            "focus": focus,
            "window": window,
            "custom_scope": custom_scope,
            "modules": sorted({
                item.module for item in items if item.module != "custom"
            }),
            "items": [
                {
                    "id": item.id,
                    "module": item.module,
                    "section": item.section,
                    "title": item.title,
                }
                for item in items
            ],
        })]
        entries: list[dict] = []
        failures: list[dict] = []
        seen: set[str] = set()

        for item in items:
            if item.id == "cx1" and custom_failure:
                failures.append({
                    "id": item.id,
                    "section": item.section,
                    "title": item.title,
                    "reason": custom_failure,
                })
                events.append(("analysis.section.failed", failures[-1]))
                continue
            try:
                sub_events, entry = self._run_subquestion(
                    state, item.id, item.title, item.slots,
                    preferred=not entries,
                    analysis_section=item.section,
                    analysis_item=item.id,
                )
            except Exception as first_error:
                if item.slots.get("filters"):
                    retry_slots = dict(item.slots)
                    retry_slots.pop("filters", None)
                    try:
                        sub_events, entry = self._run_subquestion(
                            state, item.id, item.title, retry_slots,
                            preferred=not entries,
                            analysis_section=item.section,
                            analysis_item=item.id,
                        )
                    except Exception as exc:
                        reason = str(exc)
                    else:
                        reason = None
                        events.append(("analysis.filter_dropped", {
                            "id": item.id,
                            "reason": "会话筛选不适用于该板块，已按当前数据源全量口径计算",
                        }))
                else:
                    reason = str(first_error)
                if reason:
                    failures.append({
                        "id": item.id,
                        "section": item.section,
                        "title": item.title,
                        "reason": reason,
                    })
                    events.append(("analysis.section.failed", failures[-1]))
                    continue

            if entry["artifact"].fingerprint in seen:
                continue
            seen.add(entry["artifact"].fingerprint)
            events.extend(sub_events)
            insights = build_insights(
                entry["plan"], entry["artifact"].columns, entry["artifact"].rows,
                entry["artifact"].row_count, entry["artifact"].truncated,
                entry["issues"], self.sem,
            )
            entry["section"] = item.section
            entry["title"] = item.title
            entry["insights"] = insights
            entries.append(entry)
            events.append(("analysis.section", {
                "id": item.id,
                "section": item.section,
                "title": item.title,
                "result_id": entry["artifact"].result_id,
                "insights": insights,
                "issues": entry["issues"],
            }))

        if not entries:
            events.append(("error.raised", {
                "code": "ANALYSIS_EMPTY",
                "message": "所有分析板块都未能生成可执行结果。",
                "recoverable": False,
            }))
            return events

        llm_context = {
            "dataset": self.sem.dataset,
            "focus": focus,
            "window": window,
            "brief": {
                "focus_text": custom_text,
                "modules": sorted({entry["section"] for entry in entries}),
                "custom_title": custom_title,
            },
            "sections": [
                {
                    "id": entry["id"],
                    "section": entry["section"],
                    "title": entry["title"],
                    "metric": self.sem.metric(entry["plan"].metric).name
                    if entry["plan"].metric else None,
                    "columns": entry["artifact"].columns,
                    "rows": entry["artifact"].rows[:8],
                    "insights": entry["insights"],
                    "caveats": [
                        issue.get("title") or issue.get("message")
                        for issue in entry["issues"]
                        if issue.get("severity") != "info"
                    ][:4],
                }
                for entry in entries
            ],
            "failures": [
                {
                    **failure,
                    "title": re.sub(
                        r"[（(]an\d+[）)]", "", str(failure.get("title") or "")
                    ).strip(),
                    "reason": str(failure.get("reason") or "").replace(
                        "capability refused", "当前数据不支持该分析板块"
                    ),
                }
                for failure in failures
            ],
        }
        llm = self.analysis_writer.write(llm_context)
        report, core = self._build_analysis_report(
            focus, window, entries, failures, llm,
        )
        result_id = entries[0]["artifact"].result_id
        section_count = len({entry["section"] for entry in entries})
        summary = (
            f"经营分析已完成：{section_count} 个板块、{len(entries)} 个查询"
            + (f"，{len(failures)} 个板块未完成" if failures else "")
        )
        chat_lines = [summary]
        if core:
            chat_lines.extend(f"- {line}" for line in core[:3])
        quality_notes = [
            item for item in ((self.quality or {}).get("checks") or [])
            if item.get("severity") in {"warn", "error"}
            and (item.get("count") or 0) > 0
        ]
        if quality_notes:
            chat_lines.append(
                "数据提示："
                + "；".join(
                    f"{item.get('title')}（{item.get('count')}）"
                    for item in quality_notes[:3]
                )
                + "。结果按可用记录计算，异常或缺失数据可能影响结论。"
            )
        chat_lines.append("完整报告已放在左侧「经营分析」标签中。")
        events.append(("analysis.completed", {
            "report": report,
            "summary": summary,
            "result_id": result_id,
            "writer": "llm" if llm else "template",
            "core_conclusions": core,
            "watchouts": (llm or {}).get("watchouts", []),
            "sections": [
                {
                    "id": entry["id"],
                    "section": entry["section"],
                    "title": entry["title"],
                    "result_id": entry["artifact"].result_id,
                    "insights": entry["insights"],
                }
                for entry in entries
            ],
            "failures": failures,
            "window": window,
        }))
        events.append(("answer.composed", {
            "text": self._with_draft_notice("\n".join(chat_lines)),
            "result_id": result_id,
            "kind": "analysis",
            "semantic_status": self._semantic_status(),
            "options": [],
        }))
        events.append(("result.presented", {"result_id": result_id}))
        return events

    def _build_analysis_report(self, focus: dict, window: dict,
                               entries: list[dict], failures: list[dict],
                               llm: dict | None) -> tuple[str, list[str]]:
        from app.llm.compose_answer import fmt_number

        lines = ["# 经营分析报告", ""]
        if focus.get("focus_label"):
            lines.append(f"> 会话聚焦：{focus['focus_label']}")
        else:
            lines.append(
                f"> 数据覆盖：{self.sem.time_range[0]} 至 {self.sem.time_range[1]}"
            )
        lines.append("")
        commentary = (llm or {}).get("sections") or {}
        core = list((llm or {}).get("core_conclusions") or [])
        if not core:
            for entry in entries:
                if entry["insights"]:
                    core.append(entry["insights"][0])
                if len(core) >= 4:
                    break
        if core:
            lines.extend(["## 核心结论", ""])
            lines.extend(f"- {point}" for point in core[:4])
            lines.append("")

        current_section = None
        by_section: dict[str, list[dict]] = {}
        for entry in entries:
            by_section.setdefault(entry["section"], []).append(entry)
        for section, section_entries in by_section.items():
            lines.extend([f"## {section}", ""])
            for entry in section_entries:
                plan = entry["plan"]
                artifact = entry["artifact"]
                metric = self._metric_for_plan(plan)
                unit = metric.raw.get("unit") if metric else None
                lines.extend([f"### {entry['title']}", ""])
                if commentary.get(entry["id"]):
                    lines.extend([commentary[entry["id"]], ""])
                for insight in entry["insights"][:3]:
                    lines.append(f"- {insight}")
                if entry["insights"]:
                    lines.append("")
                if artifact.rows:
                    shown_columns = artifact.columns[:6]
                    lines.append("| " + " | ".join(shown_columns) + " |")
                    lines.append("| " + " | ".join("---" for _ in shown_columns) + " |")
                    for row in artifact.rows[:6]:
                        values = []
                        for index, value in enumerate(row[:len(shown_columns)]):
                            column_type = (
                                artifact.column_types[index]
                                if index < len(artifact.column_types) else ""
                            ).upper()
                            is_text = any(
                                token in column_type
                                for token in ("CHAR", "TEXT", "BOOL", "JSON", "UUID")
                            )
                            if value is None:
                                values.append("—")
                            elif is_text:
                                values.append(str(value))
                            else:
                                try:
                                    values.append(fmt_number(value, unit))
                                except (TypeError, ValueError):
                                    values.append(str(value))
                        lines.append("| " + " | ".join(values) + " |")
                    lines.append("")
                lines.append(f"结果记录：`{artifact.result_id}`")
                caveats = [
                    issue.get("title") or issue.get("message")
                    for issue in entry["issues"]
                    if issue.get("severity") != "info"
                ]
                if caveats:
                    lines.append("口径提示：" + "；".join(caveats[:4]))
                lines.append("")

        if failures:
            lines.extend(["## 未完成的板块", ""])
            for failure in failures:
                title = re.sub(
                    r"[（(]an\d+[）)]", "", str(failure.get("title") or "")
                ).strip()
                reason = str(failure.get("reason") or "").replace(
                    "capability refused", "当前数据不支持该分析板块"
                )
                lines.append(f"- {title}：{reason}")
            lines.append("")
        watchouts = (llm or {}).get("watchouts") or []
        if watchouts:
            lines.extend(["## 需要关注", ""])
            lines.extend(f"- {item}" for item in watchouts[:4])
            lines.append("")
        checks = (self.quality or {}).get("checks") or []
        notable = [
            f"{item.get('title')}：{item.get('count')}"
            for item in checks
            if item.get("count") and item.get("severity") in {"warn", "error"}
        ][:5]
        if notable:
            lines.extend(["## 数据质量提示", ""])
            lines.extend(f"- {item}" for item in notable)
            lines.append("")
        lines.append(
            "> 报告中的数值均来自已执行的只读查询；经营判断不替代业务确认。"
        )
        return "\n".join(lines).strip(), core[:4]

    def _multi_interpretation_events(self, state: SessionState, result) -> list[tuple[str, dict]]:
        """Run each plausible interpretation and let the composer present them.

        This is a deliberate sub-pipeline: all events are still appended to the
        same log, but the two plans execute in one step instead of re-entering
        the single-query state machine.
        """
        preferred_id = result.preferred_interpretation
        interps = sorted(
            result.interpretations,
            key=lambda i: (i["id"] != preferred_id, -float(i.get("confidence", 0))),
        )
        events: list[tuple[str, dict]] = []
        executed: list[dict] = []
        rejections: list[str] = []
        filter_failure: tuple[dict, str] | None = None
        question = self._last_user_text(state)

        for interp in interps:
            iid = interp["id"]
            is_pref = iid == preferred_id
            try:
                interp_slots = dict(interp.get("slots") or {})
                self._salvage_derived_filter(question, interp_slots)
                self._salvage_time_axis(question, interp_slots)
                self._salvage_share(question, interp_slots)
                self._salvage_ratio_expression(question, interp_slots)
                self._drop_implicit_dims_for_comparison(question, interp_slots)
                slots = Slots.model_validate(
                    self._normalize_slots_payload(interp_slots)
                )
                if not slots.visualization and result.visualization:
                    slots.visualization = result.visualization
                if slots.unresolved:
                    raise SemanticError(f"未绑定词: {', '.join(slots.unresolved)}")
                grounded = self.grounder.ground(
                    slots.model_dump(mode="json"), slots.unresolved, project_for_parser(state, dim_values_path=self.dim_values_path)
                )
                events.extend(self._tool_events(
                    "members.ground",
                    {
                        "interpretation_id": iid,
                        "filters": slots.model_dump(mode="json").get("filters") or {},
                    },
                    ToolResult(
                        "members.ground",
                        not bool(grounded.ambiguity),
                        {
                            "bindings": grounded.bindings,
                            "notes": grounded.notes,
                            "ambiguity": grounded.ambiguity,
                        },
                        error_code="AMBIGUOUS_MEMBER" if grounded.ambiguity else None,
                        message=(
                            grounded.ambiguity.get("message", "")
                            if grounded.ambiguity else ""
                        ),
                        repairable=bool(grounded.ambiguity),
                    ),
                ))
                if grounded.ambiguity:
                    raise SemanticError(grounded.ambiguity.get("message", "值绑定失败"))
                for binding in grounded.bindings:
                    slots.filters[binding["dim"]] = FilterSpec(
                        op=binding.get("op", "in"), value=binding["ids"]
                    )
                self._infer_limit_per_group(question, slots)
                plan = plan_from_ir(QueryIR.from_slots(slots), self.sem)
                plan.interpretation_id = iid
                plan.interpretation_label = interp.get("label")
                plan.preferred = is_pref
            except Exception as exc:
                rejections.append(str(exc))
                events.append(("interpretation.rejected", {
                    "interpretation_id": iid, "label": interp.get("label"),
                    "message": str(exc),
                }))
                continue
            events.append(("plan.compiled", {
                "plan": plan.model_dump(mode="json"),
                "interpretation_id": iid,
                "interpretation_label": interp.get("label"),
                "preferred": is_pref,
            }))
            try:
                compiled, issues = prepare(plan, self.sem)
            except PipelineError as exc:
                if any(i.code == "UNSUPPORTED_FILTER" for i in exc.issues):
                    filter_failure = (interp.get("slots") or {}, str(exc))
                degraded = None
                if any(i.code == "UNSUPPORTED_GRAIN" for i in exc.issues):
                    degraded = degrade_unsupported_dimensions(plan, self.sem)
                if degraded:
                    new_plan, removed = degraded
                    try:
                        compiled, issues = prepare(new_plan, self.sem)
                    except PipelineError:
                        degraded = None
                    else:
                        plan = new_plan
                        events.append(("plan.degraded", {
                            "reason": plan.degraded_reason,
                            "removed_dimensions": removed,
                            "interpretation_id": iid,
                        }))
                        events.append(("plan.compiled", {
                            "plan": plan.model_dump(mode="json"),
                            "interpretation_id": iid,
                            "interpretation_label": interp.get("label"),
                            "preferred": is_pref,
                            "degraded": True,
                        }))
                if not degraded:
                    events.append(("interpretation.rejected", {
                        "interpretation_id": iid, "label": interp.get("label"),
                        "message": str(exc),
                    }))
                    continue
            except Exception as exc:
                # A bad model-generated interpretation must not turn the whole
                # turn into "system internal error". Other interpretations can
                # still answer (SESSION 2FF8B7 Turn 11).
                log_event(
                    logger, "interpretation.error",
                    interpretation_id=iid,
                    error=f"{type(exc).__name__}: {exc}",
                    detail=traceback.format_exc()[:2000],
                )
                events.append(("interpretation.rejected", {
                    "interpretation_id": iid,
                    "label": interp.get("label"),
                    "message": str(exc),
                }))
                continue
            events.append(("sql.compiled", {
                "sql": compiled.sql,
                "params": [str(p) for p in compiled.params],
                "fingerprint": compiled.fingerprint(self.sem.version),
                "columns": compiled.columns,
                "tables_used": compiled.tables_used,
                "meta": compiled.meta,
                "issues": [i.as_dict() for i in issues if i.severity != "error"],
                "interpretation_id": iid,
                "preferred": is_pref,
            }))
            try:
                artifact = execute_prepared(compiled, db_path=self.warehouse)
            except Exception as exc:
                events.append(("interpretation.rejected", {
                    "interpretation_id": iid, "label": interp.get("label"),
                    "message": str(exc),
                }))
                continue
            artifact.fingerprint = compiled.fingerprint(
                int(self.sem.raw.get("compiler_version", 1)),
                self.sem.version,
            )
            artifact.result_id = f"r{state.turn}-{iid}-{artifact.fingerprint}"
            self._results[(state.session_id, state.turn, iid)] = artifact
            payload = artifact.as_dict()
            display = display_spec(plan, self.sem, artifact)
            payload.update({
                "question": question,
                "display": display,
                "chart": recommend_chart(plan, self.sem, display, artifact),
                "interpretation_id": iid,
                "interpretation_label": interp.get("label"),
                "preferred": is_pref,
                "plan": plan.model_dump(mode="json"),
            })
            payload["field_map"] = self._field_map(plan)
            events.append(("sql.executed", payload))
            issues, facts = check(plan, self.sem, artifact,
                                  db_path=self.warehouse, quality=self.quality,
                                  catalog=getattr(self.ctx, "catalog", None))
            events.extend(self._enrich_count_members(plan, artifact, facts))
            events.append(("checks.completed", {
                "result_id": artifact.result_id,
                "issues": [i.as_dict() for i in issues],
                "facts": facts,
                "blocked": any(i.severity == "error" for i in issues),
                "interpretation_id": iid,
                "interpretation_label": interp.get("label"),
                "preferred": is_pref,
            }))
            executed.append({
                "id": iid,
                "label": interp.get("label"),
                "preferred": is_pref,
                "plan": plan,
                "artifact": artifact,
                "issues": [i.as_dict() for i in issues],
                "facts": facts,
            })

        if not executed:
            if filter_failure:
                slots, reason = filter_failure
                repair = build_repair(self.sem, self._repair_slots(state, slots))
                message = f"{reason}\n\n{repair_text(repair)}"
                events.append(("answer.composed", {
                    "text": message, "result_id": None, "options": repair["options"],
                }))
                events.append(("result.presented", {"result_id": None}))
                return events
            reason = next((item for item in rejections if item), "")
            message = (
                f"这几种理解都无法生成可执行的查询：{reason}"
                if reason else "这几种理解都无法生成可执行的查询。"
            )
            message += "\n\n" + self._suggestions()
            events.append(("error.raised", {
                "code": "NO_INTERPRETATION",
                "message": message,
                "recoverable": False,
            }))
            return events

        ctx = self._multi_composer_context(state, executed, result.ambiguity_note)
        text = self._with_draft_notice(self._compose(ctx, state))
        options: list[str] = []
        if filter_failure:
            slots, reason = filter_failure
            repair = build_repair(self.sem, self._repair_slots(state, slots))
            options = repair["options"]
            text = f"{text}\n\n补充说明：{reason}\n\n{repair_text(repair)}"
        events.append(("answer.composed", {
            "text": text,
            "result_id": next(
                (e["artifact"].result_id for e in executed if e["preferred"]),
                executed[0]["artifact"].result_id,
            ),
            "interpretations": [
                {"id": e["id"], "label": e["label"], "preferred": e["preferred"]}
                for e in executed
            ],
            "options": options,
            "semantic_status": self._semantic_status(),
        }))
        events.append(("result.presented", {
            "result_id": next(
                (e["artifact"].result_id for e in executed if e["preferred"]),
                executed[0]["artifact"].result_id,
            ),
        }))
        return events

    def _multi_composer_context(self, state: SessionState, executed: list[dict],
                                ambiguity_note: str | None = None) -> dict:
        interps = []
        for e in executed:
            plan = e["plan"]
            metric = self._metric_for_plan(plan)
            interps.append({
                "id": e["id"],
                "label": e["label"],
                "preferred": e["preferred"],
                "metric_name": metric.name,
                "metric_unit": metric.raw.get("unit"),
                "metric_type": metric.type,
                "definition": metric.raw.get("definition"),
                "dimensions": plan.dimensions,
                "dimension_labels": [
                    self.sem.dimension(d).raw.get("name_zh", d) for d in plan.dimensions
                ],
                "time": plan.time.model_dump(mode="json") if plan.time else None,
                "comparison": plan.comparison.model_dump(mode="json") if plan.comparison else None,
                "filters": {d: s.model_dump(mode="json") for d, s in plan.filters.items()},
                "filters_label": self._plan_filters_label(plan),
                "columns": e["artifact"].columns,
                "rows": e["artifact"].rows,
                "row_count": e["artifact"].row_count,
                "truncated": e["artifact"].truncated,
                "meta": e["artifact"].meta,
                "checks": e["issues"],
                "facts": e["facts"],
            })
        return {
            "session_id": state.session_id,
            "question": self._last_user_text(state),
            "interpretations": interps,
            "preferred": next((i["id"] for i in interps if i["preferred"]), interps[0]["id"]),
            "ambiguity_note": ambiguity_note,
        }

    def _compose(self, ctx: dict, state: SessionState) -> str:
        events: list[tuple[str, dict]] = []
        try:
            text = self._call_with_retry(
                "compose_answer", lambda: self.composer.compose(ctx), state, events
            )
        except Exception as exc:
            from app.llm.compose_answer import TemplateComposer
            log_event(logger, "composer fallback", error=str(exc))
            text = TemplateComposer().compose(ctx)
        return self._humanize_fields(text, ctx.get("field_map") or [])

    def _handle_grounding(self, state: SessionState) -> list[tuple[str, dict]]:
        ctx = project_for_parser(state, dim_values_path=self.dim_values_path)
        grounded = self.grounder.ground(
            state.slots.model_dump(mode="json"), state.slots.unresolved, ctx
        )
        events = self._tool_events(
            "members.ground",
            {
                "filters": state.slots.model_dump(mode="json").get("filters") or {},
                "unresolved": list(state.slots.unresolved),
            },
            ToolResult(
                "members.ground",
                not bool(grounded.ambiguity),
                {
                    "bindings": grounded.bindings,
                    "notes": grounded.notes,
                    "ambiguity": grounded.ambiguity,
                },
                error_code="AMBIGUOUS_MEMBER" if grounded.ambiguity else None,
                message=(
                    grounded.ambiguity.get("message", "")
                    if grounded.ambiguity else ""
                ),
                repairable=bool(grounded.ambiguity),
            ),
        )
        if grounded.ambiguity:
            payload = dict(grounded.ambiguity)
            payload["partial_slots"] = state.slots.model_dump(mode="json")
            return events + [("ambiguity.detected", payload)]
        return events + [("values.grounded", {
            "bindings": grounded.bindings,
            "notes": grounded.notes,
        })]

    def _salvage_derived_filter(self, text: str, slots: dict) -> None:
        """Map label words (延迟/准时) onto a declared derived dimension.

        Only when the label is a real constraint:
        - never for a ratio metric whose numerator already encodes the predicate;
        - never when the metric expression already contains the predicate;
        - never when both labels appear (the user wants a split, not a filter).
        """
        metric_id = slots.get("metric")
        metric = self.sem.metrics.get(metric_id) if metric_id else None
        if metric is not None and metric.type == "ratio":
            return
        expr_text = ""
        if metric is not None:
            expr_text = " ".join(
                str(metric.raw.get(key) or "")
                for key in ("expr", "numerator", "grain_numerator")
            )
        compact_expr = re.sub(r"\s+", "", expr_text)
        for dim_id, dim in self.sem.dimensions.items():
            if dim.kind != "derived":
                continue
            labels = dim.raw.get("labels") or {}
            values = [v for v in (labels.get("true"), labels.get("false")) if v]
            present = [str(v) for v in values if str(v) in (text or "")]
            if len(present) != 1:
                continue
            dim_expr = str(dim.raw.get("expr") or "")
            if dim_expr and re.sub(r"\s+", "", dim_expr) in compact_expr:
                continue
            value = present[0]
            filters = slots.setdefault("filters", {})
            filters.setdefault(dim_id, {"op": "eq", "value": value})
            slots["unresolved"] = [
                term for term in (slots.get("unresolved") or [])
                if str(value) not in str(term)
            ]




    @staticmethod
    def _degraded_kind(degradations: list[dict]) -> str | None:
        kinds = {str(item.get("kind")) for item in degradations if item.get("kind")}
        if not kinds:
            return None
        return "time" if kinds == {"time"} else "dimension"



    def _salvage_ratio_expression(self, text: str, slots: dict) -> None:
        """Resolve explicit "A / B" syntax to two defined metrics."""
        if slots.get("ratio_denominator"):
            return
        text = str(text or "").strip()
        patterns = (
            (r"(.+?)\s*(?:除以|/)\s*(.+)", False),
            (r"(.+?)\s*占\s*(.+?)\s*的?比例", False),
            (r"每\s*(.+?)\s*的\s*(.+)", True),
        )
        for pattern, swap in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            left, right = match.group(1), match.group(2)
            if swap:
                left, right = right, left
            numerator = self._match_metric_text(left)
            denominator = self._match_metric_text(right)
            if numerator is None or denominator is None or numerator.id == denominator.id:
                continue
            slots["metric"] = numerator.id
            slots["ratio_numerator"] = numerator.id
            slots["ratio_denominator"] = denominator.id
            slots["notes"] = list(slots.get("notes") or []) + [
                f"按显式比率解析：{numerator.name} / {denominator.name}"
            ]
            return

    def _match_metric_text(self, text: str):
        text = str(text or "").strip()
        hits: list[tuple[int, object]] = []
        for metric in self.sem.metrics.values():
            for term in [metric.name, *metric.synonyms]:
                term = str(term or "")
                if term and term in text:
                    hits.append((len(term), metric))
        if not hits:
            return None
        return max(hits, key=lambda item: item[0])[1]

    def _drop_implicit_dims_for_comparison(self, text: str, slots: dict) -> None:
        """A period comparison without an explicit breakdown compares totals."""
        if not (slots.get("compare") or slots.get("comparison")):
            return
        dims = list(slots.get("dimensions") or [])
        if not dims:
            return
        for dim_id in dims:
            dim = self.sem.dimensions.get(dim_id)
            if dim is None:
                continue
            terms = [str(dim.raw.get("name_zh") or "")]
            terms += [str(term) for term in getattr(dim, "synonyms", []) or []]
            if any(term and term in (text or "") for term in terms):
                return
        slots["dimensions"] = []
        slots["limit_per_group"] = None
        slots["order_by"] = []
        slots["notes"] = list(slots.get("notes") or []) + [
            "比较问题未指定分组，清空上一轮继承的维度"
        ]

    def _salvage_share(self, text: str, slots: dict) -> None:
        """Map "占比/份额/比重" to the share algebra when it is applicable."""
        if slots.get("share"):
            return
        if not re.search(r"占比|份额|比重", text or ""):
            return
        metric_id = slots.get("metric")
        metric = self.sem.metrics.get(metric_id) if metric_id else None
        if metric is None or metric.type != "aggregate" or not metric.raw.get("additive"):
            return
        if not (slots.get("dimensions") or []):
            return
        slots["share"] = True

    def _salvage_time_axis(self, text: str, slots: dict) -> None:
        """Map "按下单月/按送达月" wording to a declared time axis."""
        if slots.get("time_axis"):
            return
        patterns = (
            (r"(按下单|下单月|下单月份|下单时间)", "下单时间"),
            (r"(按送达|送达月|送达月份|送达时间)", "送达时间"),
            (r"(按评价|评价月|评价月份|评价时间)", "评价时间"),
            (r"(按支付|支付月|支付月份|支付时间)", "支付时间"),
            (r"(按发货|发货月|发货月份|发货时间)", "发货时间"),
        )
        for pattern, axis_name in patterns:
            if not re.search(pattern, text or ""):
                continue
            for axis in self.sem.time_axes:
                if axis.get("name_zh") != axis_name:
                    continue
                slots["time_axis"] = axis.get("ref")
                slots["unresolved"] = [
                    term for term in (slots.get("unresolved") or [])
                    if not re.search(pattern, str(term))
                ]
                return

    @staticmethod
    def _infer_limit_per_group(question: str, slots: Slots) -> None:
        """Offline fallback for \"每个X最好的Y\": one row per group, ranked."""
        if slots.limit_per_group or len(slots.dimensions) < 2:
            return
        if re.search(
            r"(每个|各|按).{0,16}(最好|最高|最大|第一|排名|top)",
            question or "",
            re.IGNORECASE,
        ):
            slots.limit_per_group = 1

    def _handle_planning(self, state: SessionState) -> list[tuple[str, dict]]:
        slots = Slots.model_validate(state.slots.model_dump())
        ir = state.ir or QueryIR.from_slots(slots)
        ir = self._normalize_entity_ratio_ir(ir, self._last_user_text(state))
        if (
            not ir.limit_per_group
            and len(ir.dimensions) >= 2
            and re.search(
                r"(每个|各|按).{0,16}(最好|最高|最大|第一|排名|top)",
                self._last_user_text(state) or "",
                re.IGNORECASE,
            )
        ):
            ir = replace(ir, limit_per_group=1)
        report = assess_slots(
            ir, self.sem, policy=self.policy,
            model=getattr(self.ctx, "model", None),
        )
        events: list[tuple[str, dict]] = [("capability.assessed", report.as_dict())]

        if report.refused:
            return events + [
                ("refusal.decided", report.refusal_payload()),
                ("answer.composed", {
                    "text": render_refusal(report, self.sem),
                    "result_id": None,
                    "status": "refused",
                }),
                ("result.presented", {"result_id": None}),
            ]

        try:
            if slots.intent == "lookup":
                plan = member_list_plan(
                    slots.dimensions[0] if slots.dimensions else None, slots, self.sem
                )
            else:
                if report.degradations:
                    slots = apply_degradations(slots, report)
                    ir = QueryIR.from_slots(slots)
                plan = plan_from_ir(ir, self.sem)
                if report.degradations:
                    reasons = [
                        str(d.get("reason")) for d in report.degradations
                        if d.get("reason")
                    ]
                    plan.degraded_reason = "；".join(dict.fromkeys(reasons)) or None
                    plan.degraded_kind = self._degraded_kind(report.degradations)
        except SemanticError as exc:
            if ir.intent == "coverage" and self.quality is not None:
                return events + [
                    ("answer.composed", {
                        "text": self._quality_fallback_text(),
                        "result_id": None,
                        "status": "quality_fallback",
                    }),
                    ("result.presented", {"result_id": None}),
                ]
            message = f"{exc}\n\n{self._suggestions()}"
            return events + [
                ("plan.rejected", {"reason": str(exc), "issues": [], "recoverable": False}),
                ("error.raised", {
                    "code": "PLAN_UNSUPPORTED", "message": message, "recoverable": False,
                }),
            ]
        return events + [("plan.compiled", {"plan": plan.model_dump(mode="json")})]

    def _handle_validating(self, state: SessionState) -> list[tuple[str, dict]]:
        plan = QueryPlan.model_validate(state.plan.model_dump())
        events = self._tool_events(
            "plan.validate",
            {"plan": plan.model_dump(mode="json")},
            ToolResult("plan.validate", True, {"issues": []}),
        )
        try:
            compiled, issues = prepare(plan, self.sem)
        except PipelineError as exc:
            issue_dicts = [issue.as_dict() for issue in exc.issues]
            events[-1] = (
                "tool.result",
                ToolResult(
                    "plan.validate",
                    False,
                    {"issues": issue_dicts},
                    error_code=exc.issues[0].code if exc.issues else "PLAN_INVALID",
                    message=str(exc),
                    repairable=True,
                ).as_dict(),
            )
            hard_codes = {
                "ENTITY_COUNT_MISMATCH",
                "ENTITY_COUNT_HAS_HAVING",
                "ENTITY_COUNT_MISSING_THRESHOLD",
                "NESTED_WITHOUT_GROUP",
            }
            if plan.entity_count or any(
                issue.get("code") in hard_codes for issue in issue_dicts
            ):
                message = "；".join(
                    str(issue.get("message") or issue.get("code"))
                    for issue in issue_dicts
                )
                return events + [
                    ("plan.rejected", {
                        "reason": str(exc),
                        "issues": issue_dicts,
                        "recoverable": False,
                    }),
                    ("answer.composed", {
                        "text": message,
                        "result_id": None,
                        "status": "rejected",
                    }),
                    ("result.presented", {"result_id": None}),
                ]
            repaired_plan, repair_details = self.tools.repair_nested_plan(
                plan, issue_dicts
            )
            if repaired_plan is not None:
                repair_events = self._tool_events(
                    "plan.repair",
                    {"plan": plan.model_dump(mode="json"), "issues": issue_dicts},
                    ToolResult("plan.repair", True, repair_details),
                )
                try:
                    repaired_compiled, repaired_issues = prepare(
                        repaired_plan, self.sem
                    )
                except PipelineError as repaired_exc:
                    log_event(
                        logger,
                        "plan.repair.failed",
                        error=str(repaired_exc),
                    )
                else:
                    return (
                        events
                        + repair_events
                        + [
                            ("plan.repaired", repair_details),
                            ("plan.compiled", {
                                "plan": repaired_plan.model_dump(mode="json"),
                                "repaired": True,
                            }),
                            ("sql.compiled", self._compiled_payload(
                                repaired_compiled, repaired_issues
                            )),
                        ]
                    )
            filter_issue = next(
                (i for i in exc.issues if i.code == "UNSUPPORTED_FILTER"), None
            )
            if filter_issue:
                repair = build_repair(
                    self.sem, self._repair_slots(state, state.slots.model_dump(mode="json"))
                )
                message = f"{filter_issue.message}\n\n{repair_text(repair)}"
                return events + [
                    ("plan.rejected", {
                        "reason": str(exc),
                        "issues": [i.as_dict() for i in exc.issues],
                        "recoverable": False,
                    }),
                    ("answer.composed", {
                        "text": message, "result_id": None, "options": repair["options"],
                    }),
                    ("result.presented", {"result_id": None}),
                ]
            # Unsupported grain is recoverable: keep a verifiable overall
            # result and let the answer say what was dropped.
            degraded = None
            if any(i.code == "UNSUPPORTED_GRAIN" for i in exc.issues):
                degraded = degrade_unsupported_dimensions(plan, self.sem)
            if degraded:
                new_plan, removed = degraded
                try:
                    compiled, issues = prepare(new_plan, self.sem)
                except PipelineError:
                    pass
                else:
                    return events + [
                        ("plan.degraded", {
                            "reason": new_plan.degraded_reason,
                            "removed_dimensions": removed,
                            "original_plan": plan.model_dump(mode="json"),
                        }),
                        ("plan.compiled", {
                            "plan": new_plan.model_dump(mode="json"),
                            "degraded": True,
                        }),
                        ("sql.compiled", self._compiled_payload(compiled, issues)),
                    ]
            repair_method = getattr(self.parser, "repair", None)
            if callable(repair_method) and state.policy.can_retry("plan_repair"):
                observations = []
                search = self.tools.schema_search(
                    f"{plan.metric or ''} {' '.join(plan.dimensions)}".strip()
                )
                observations.extend(self._tool_events(
                    "schema.search",
                    {"query": plan.metric or "", "context": "plan_repair"},
                    search,
                ))
                metric_result = self.tools.get_metric(plan.metric)
                observations.extend(self._tool_events(
                    "schema.get_metric", {"metric_id": plan.metric}, metric_result,
                ))
                dim_results = []
                for dim_id in plan.dimensions:
                    dim_result = self.tools.get_dimension(dim_id)
                    dim_results.append(dim_result)
                    observations.extend(self._tool_events(
                        "schema.get_dimension", {"dimension_id": dim_id}, dim_result,
                    ))
                tool_results = [
                    search.as_dict(), metric_result.as_dict(),
                    *[result.as_dict() for result in dim_results],
                ]
                try:
                    repaired = repair_method(
                        self._last_user_text(state),
                        project_for_parser(
                            state,
                            dim_values_path=self.dim_values_path,
                            conversation_context=build_conversation_context(
                                state, self.sem
                            ),
                        ),
                        plan=plan.model_dump(mode="json"),
                        issues=issue_dicts,
                        tool_results=tool_results,
                    )
                except Exception as exc:
                    log_event(
                        logger,
                        "plan.repair.llm_failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    if repaired is not None:
                        repaired_slots = self._normalize_slots_payload(
                            dict(repaired.slots or {})
                        )
                        return events + observations + [
                            ("llm.retry", {
                                "stage": "plan_repair",
                                "attempt": 1,
                                "reason": str(exc),
                            }),
                            ("plan.repair_requested", {
                                "repairer": "llm",
                                "issues": issue_dicts,
                                "tool_results": tool_results,
                            }),
                            ("intent.repaired", {
                                "slots": repaired_slots,
                                "notes": list(repaired.notes or []) + [
                                    "计划校验失败后已由工具辅助修复"
                                ],
                            }),
                        ]
            return events + [
                ("plan.rejected", {
                    "reason": str(exc),
                    "issues": [i.as_dict() for i in exc.issues],
                    "recoverable": False,
                }),
                ("error.raised", {
                    "code": "PLAN_INVALID", "message": str(exc), "recoverable": False,
                }),
            ]
        events[-1] = (
            "tool.result",
            ToolResult(
                "plan.validate",
                True,
                {"issues": [issue.as_dict() for issue in issues]},
            ).as_dict(),
        )
        return events + [("sql.compiled", self._compiled_payload(compiled, issues))]

    def _compiled_payload(self, compiled, issues) -> dict:
        return {
            "sql": compiled.sql,
            "params": [str(p) for p in compiled.params],
            "fingerprint": compiled.fingerprint(self.sem.version),
            "columns": compiled.columns,
            "tables_used": compiled.tables_used,
            "meta": compiled.meta,
            "issues": [i.as_dict() for i in issues if i.severity != "error"],
        }

    def _handle_executing(self, state: SessionState) -> list[tuple[str, dict]]:
        plan = QueryPlan.model_validate(state.plan.model_dump())
        try:
            compiled, _ = prepare(plan, self.sem)
            result = execute_prepared(compiled, db_path=self.warehouse)
        except PipelineError as exc:
            return self._recover_or_refuse(state, plan, exc, "PLAN_REJECTED", "计划未通过校验")
        except Exception as exc:
            log_event(logger, "execution.error", error=f"{type(exc).__name__}: {exc}",
                      detail=traceback.format_exc()[:4000])
            code, friendly = classify_error(exc)
            return self._recover_or_refuse(state, plan, exc, code, friendly)
        result.fingerprint = compiled.fingerprint(
            int(self.sem.raw.get("compiler_version", 1)),
            self.sem.version,
        )
        result.result_id = f"r{state.turn}-{result.fingerprint}"
        policy = stabilize_ratio_ranking(plan, result)
        if policy:
            result.meta["ratio_sample_policy"] = policy
        self._results[(state.session_id, state.turn)] = result
        payload = result.as_dict()
        payload["question"] = self._last_user_text(state)
        display = display_spec(plan, self.sem, result)
        payload["display"] = display
        payload["chart"] = recommend_chart(plan, self.sem, display, result)
        payload["plan"] = plan.model_dump(mode="json")
        payload["field_map"] = self._field_map(plan)
        return [("sql.executed", payload)]

    def _numeric_columns_hint(self, plan: QueryPlan) -> str:
        card = getattr(self.ctx, "model_card", {}) or {}
        spec = plan.metric_spec or {}
        fact = str(spec.get("fact") or "")
        if not fact:
            match = re.search(
                r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z0-9_]+",
                str(spec.get("expr") or ""),
            )
            if match:
                fact = match.group(1)
        table = (card.get("tables") or {}).get(fact) or {}
        measures = [
            col for col in table.get("columns") or []
            if str(col.get("role")) == "measure"
        ][:5]
        if not measures:
            return ""
        parts = []
        for column in measures:
            detail = f"{column.get('name')}({column.get('label')}, {column.get('type')}"
            if column.get("min") is not None:
                detail += f", {column.get('min')}..{column.get('max')}"
            detail += ")"
            parts.append(detail)
        return f"同表（{fact}）可用的数值列：" + "、".join(parts)

    def _recover_or_refuse(self, state: SessionState, plan: QueryPlan,
                           exc: BaseException, code: str, friendly: str
                           ) -> list[tuple[str, dict]]:
        """Bounded, deterministic self-correction. SQL is never regenerated by a model."""
        events: list[tuple[str, dict]] = [("execution.failed", {
            "code": code,
            "message": friendly,
            "attempt": state.policy.attempts + 1,
            "detail": f"{type(exc).__name__}: {exc}"[:300],
        })]
        if state.policy.can_attempt() and code != "TYPE_MISMATCH":
            for candidate in candidates_for_failure(plan, self.sem):
                try:
                    prepare(candidate.plan, self.sem)
                except PipelineError:
                    continue
                events.append(("repair.applied", {
                    "action": candidate.action,
                    "attempt": state.policy.attempts + 1,
                    "detail": candidate.detail,
                }))
                events.append(("plan.compiled", {
                    "plan": candidate.plan.model_dump(mode="json"),
                    "repaired": True,
                }))
                return events

        labels = {
            "drop_having": "取消分组筛选",
            "drop_comparison": "取消比较口径",
            "drop_dimension": "移除无法执行的维度",
            "drop_all_dimensions": "退化为整体口径",
            "widen_time": "放宽时间范围",
        }
        tried = [str(item.get("action")) for item in state.repairs]
        tried_labels = [labels.get(action, action) for action in tried]
        refusal = {
            "code": code,
            "missing": [{
                "kind": "data", "name": "可执行口径", "reason": friendly,
            }],
            "tried": tried,
            "alternatives": [],
        }
        text = f"这个问题的查询没能成功执行（{friendly}）。"
        if tried_labels:
            text += (
                f"已尝试 {len(tried_labels)} 次调整"
                f"（{'、'.join(tried_labels)}），仍未得到可用结果。"
            )
        if code == "SCHEMA_MISMATCH":
            text += (
                "\n\n这通常意味着语义层和数据库不一致：数据可能被重新导入或列被删除。"
                "建议在数据管理页重建或重新发布数据集。"
            )
        elif code == "TYPE_MISMATCH":
            hint = self._numeric_columns_hint(plan)
            text += (
                "\n\n这不是时间或维度问题，而是指标用错了列：聚合的列类型不支持该运算。"
                + (f"\n{hint}" if hint else "请改用同表的数值度量列。")
            )
        elif code == "TIMEOUT":
            text += "\n\n查询超时，可以缩小时间范围或减少维度后重试。"
        else:
            text += "\n\n可以缩小时间范围、减少维度，或换一个指标再试。"
        events.extend([
            ("refusal.decided", refusal),
            ("answer.composed", {
                "text": text, "result_id": None, "status": "refused",
            }),
            ("result.presented", {"result_id": None}),
        ])
        return events


    def _filtered_row_count(self, plan: QueryPlan, metric) -> int | None:
        """Rows matching the same filters/time as an aggregate query.

        Used to expose the real sample size (e.g. 2,323 reviewed late orders)
        instead of letting the model reuse a dataset-level quality count.
        """
        try:
            from dataclasses import replace

            from app.domain.semantic.loader import Metric

            raw = dict(metric.raw)
            raw["expr"] = "COUNT(*)"
            raw["type"] = "aggregate"
            raw["supported_grain"] = ["none"]
            metrics = dict(self.sem.metrics)
            metrics["__count"] = Metric("__count", raw)
            sem = replace(self.sem, metrics=metrics)
            count_plan = plan.model_copy(deep=True)
            count_plan.metric = "__count"
            count_plan.dimensions = []
            count_plan.having = []
            count_plan.compare = None
            count_plan.comparison = None
            count_plan.order_by = []
            count_plan.limit = 1
            compiled, _ = prepare(count_plan, sem)
            result = execute_prepared(compiled, db_path=self.warehouse)
            if result.rows and result.rows[0]:
                return int(result.rows[0][0])
        except Exception:
            return None
        return None

    def _handle_checking(self, state: SessionState) -> list[tuple[str, dict]]:
        plan = QueryPlan.model_validate(state.plan.model_dump())
        result = self._results.get((state.session_id, state.turn))
        if result is None:
            # replay path: deterministic re-execution
            compiled, _ = prepare(plan, self.sem)
            result = execute_prepared(compiled, db_path=self.warehouse)
            result.fingerprint = compiled.fingerprint(
                int(self.sem.raw.get("compiler_version", 1)),
                self.sem.version,
            )
            result.result_id = f"r{state.turn}-{result.fingerprint}"
            self._results[(state.session_id, state.turn)] = result
        issues, facts = check(plan, self.sem, result,
                              db_path=self.warehouse, quality=self.quality,
                              catalog=getattr(self.ctx, "catalog", None))
        policy = (result.meta or {}).get("ratio_sample_policy")
        if policy:
            issues.append(CheckIssue(
                "LOW_SAMPLE_RANK", "warn", "样本量",
                f"最高分组样本量不足：原首位只有 "
                f"{policy.get('demoted_denominator')} 笔（低于 "
                f"{policy.get('min_denominator')} 笔），已把样本充足组"
                f"（{policy.get('promoted_denominator')} 笔）置顶；"
                "小样本行仍保留在结果表中，不能当作稳定结论。",
            ))
        if plan.filters and plan.metric and plan.metric in self.sem.metrics:
            filtered_metric = self.sem.metric(plan.metric)
            if filtered_metric.type == "aggregate":
                sample_size = self._filtered_row_count(plan, filtered_metric)
                if sample_size is not None:
                    facts["contributing_rows"] = sample_size
                    issues.append(CheckIssue(
                        "SAMPLE_SIZE", "info", "样本量",
                        f"满足当前筛选条件的记录数：{sample_size}。",
                    ))
        if plan.metric and plan.metric in self.sem.metrics:
            health = self.sem.metric(plan.metric).raw.get("data_health") or {}
            rate = health.get("measure_null_rate")
            if isinstance(rate, (int, float)) and rate >= 0.2:
                issues.append(CheckIssue(
                    "DATA_HEALTH", "warn", "数据完整性",
                    f"该指标依赖的度量列缺失 {rate:.1%}，结果按可用记录计算。",
                ))
        diagnosis_events: list[tuple[str, dict]] = []
        if result.row_count == 0:
            diagnosis = diagnose_empty(plan, self.sem, self.warehouse)
            if diagnosis:
                facts["empty_diagnosis"] = diagnosis
                issues.append(CheckIssue(
                    "EMPTY_DIAGNOSIS", "warn", "空结果诊断", diagnosis["message"],
                ))
                diagnosis_events.append(("diagnosis.completed", diagnosis))
        tool_events = self._enrich_count_members(plan, result, facts)
        payload = {
            "result_id": result.result_id,
            "issues": [i.as_dict() for i in issues],
            "facts": facts,
            "blocked": any(i.severity == "error" for i in issues),
        }
        events: list[tuple[str, dict]] = diagnosis_events + tool_events + [
            ("checks.completed", payload)
        ]
        if payload["blocked"]:
            events.append(("error.raised", {
                "code": "CHECKS_FAILED",
                "message": "结果未通过业务校验，已阻止生成结论。",
                "recoverable": False,
                "issues": [i.as_dict() for i in issues if i.severity == "error"],
            }))
        return events

    def _handle_composing(self, state: SessionState) -> list[tuple[str, dict]]:
        if state.plan is not None and state.plan.kind == "coverage":
            artifact = state.artifacts.get(state.last_result_id or "") or {}
            text = self._with_draft_notice(self._coverage_answer(state, artifact))
            return [
                ("answer.composed", {
                    "text": text,
                    "result_id": state.last_result_id,
                    "semantic_status": self._semantic_status(),
                }),
                ("result.presented", {"result_id": state.last_result_id}),
            ]
        if state.plan is not None and state.plan.kind == "member_list":
            artifact = state.artifacts.get(state.last_result_id or "") or {}
            rows = artifact.get("rows") or []
            text = lookup_mod.describe(
                self.sem, state.plan.list_dim or "", rows, len(rows)
            )
            scope: list[str] = []
            if state.plan.filters:
                scope.append(self._plan_filters_label(state.plan))
            if state.plan.time:
                scope.append(f"{state.plan.time.start}~{state.plan.time.end}")
            if scope:
                text = f"限定范围：{'；'.join(scope)}。\n\n{text}"
            quality_notes = [
                issue for issue in state.checks
                if issue.get("id") in {"DATA_QUALITY", "SCOPE_TIME_DROPPED"}
            ]
            for issue in quality_notes:
                text += (
                    f"\n\n范围提示："
                    f"{issue.get('detail') or issue.get('title')}"
                )
            return [
                ("answer.composed", {"text": text, "result_id": state.last_result_id}),
                ("result.presented", {"result_id": state.last_result_id}),
            ]

        ctx = self._composer_context(state)
        events: list[tuple[str, dict]] = []
        try:
            text = self._call_with_retry("compose_answer", lambda: self.composer.compose(ctx),
                                         state, events)
        except Exception as exc:
            from app.llm.compose_answer import TemplateComposer
            log_event(logger, "composer fallback", error=str(exc))
            text = TemplateComposer().compose(ctx)
        text = self._with_draft_notice(text)
        events.append(("answer.composed", {
            "text": text,
            "result_id": state.last_result_id,
            "semantic_status": self._semantic_status(),
        }))
        events.append(("result.presented", {"result_id": state.last_result_id}))
        return events

    def _field_map(self, plan: QueryPlan | None) -> list[dict]:
        """Technical column -> business label mapping for this query.

        The answer prose must not contain `customer_unique_id`; the mapping is
        shown in the chat's "处理过程与数据说明" block instead.
        """
        if plan is None:
            return []
        card = getattr(self.ctx, "model_card", {}) or {}
        tables = card.get("tables") or {}
        metric = self._metric_for_plan(plan)
        raw = metric.raw if metric is not None else {}

        def refs(value):
            return re.findall(
                r"\b(t_[A-Za-z0-9]+)\.([A-Za-z0-9_]+)\b", str(value or "")
            )

        ordered: list[dict] = []

        def add(found) -> None:
            for table, column in found:
                field = f"{table}.{column}"
                if all(item["field"] != field for item in ordered):
                    ordered.append({"field": field, "table": table, "column": column})

        for key in (
            "expr", "numerator", "denominator", "grain_numerator",
            "grain_denominator", "operand", "time_via", "entity",
            "count_orders", "default_time_axis",
        ):
            add(refs(raw.get(key)))
        add(refs(list((raw.get("scope") or {}).keys())))
        add(refs(plan.time_axis))

        dim_labels: dict[str, str] = {}
        for dim in self.sem.dimensions.values():
            name = str(dim.raw.get("name_zh") or dim.id)
            for table, column in (
                refs(dim.raw.get("column")) + refs(dim.raw.get("expr"))
            ):
                dim_labels.setdefault(f"{table}.{column}", name)
        for dim_id in list(plan.dimensions) + list(plan.filters):
            dim = self.sem.dimensions.get(dim_id)
            if dim is None:
                continue
            name = str(dim.raw.get("name_zh") or dim_id)
            for table, column in (
                refs(dim.raw.get("column")) + refs(dim.raw.get("expr"))
            ):
                dim_labels[f"{table}.{column}"] = name
                add([(table, column)])

        axis_labels = {
            str(axis.get("ref")): str(axis.get("name_zh") or axis.get("label") or "")
            for axis in (self.sem.time_axes or [])
            if axis.get("ref")
        }

        metric_label = metric.name if metric is not None else ""
        if metric_label.startswith("平均"):
            metric_label = metric_label[2:]
        if raw.get("type") == "entity_count" and metric_label.endswith("数"):
            metric_label = metric_label[:-1]
        entity_refs = {
            f"{table}.{column}" for table, column in refs(raw.get("entity"))
        }
        metric_refs = {
            f"{table}.{column}"
            for key in (
                "expr", "numerator", "denominator", "grain_numerator",
                "grain_denominator", "operand",
            )
            for table, column in refs(raw.get(key))
        }
        field_zh = {
            "order_id": "订单号",
            "review_id": "评价编号",
            "customer_id": "客户编号",
            "customer_unique_id": "客户唯一标识",
            "order_status": "订单状态",
            "purchase_at": "下单时间",
            "approved_at": "支付时间",
            "shipped_at": "发货时间",
            "delivered_at": "送达时间",
            "estimated_delivery_at": "预计送达时间",
            "review_score": "评分",
            "payment_value": "支付金额",
            "product_id": "商品编号",
            "seller_id": "商家编号",
        }

        out: list[dict] = []
        for item in ordered:
            card_column = next(
                (
                    column for column in (tables.get(item["table"]) or {}).get("columns") or []
                    if str(column.get("name")) == item["column"]
                ),
                {},
            )
            technical = str(card_column.get("label") or item["column"])
            description = str(card_column.get("description") or "")
            if item["field"] in entity_refs:
                metric_name_label = metric_label
            elif item["field"] in metric_refs:
                metric_name_label = metric_label
            else:
                metric_name_label = ""
            label = (
                dim_labels.get(item["field"])
                or axis_labels.get(item["field"])
                or metric_name_label
                or field_zh.get(technical)
                or description
                or technical
            )
            out.append({
                "field": item["field"],
                "label": label,
                "technical": technical,
                "role": str(card_column.get("role") or ""),
                "description": description,
            })
        return out

    def _humanize_checks(self, checks, field_map: list[dict]) -> list[dict]:
        out: list[dict] = []
        for check in checks or []:
            item = dict(check)
            if item.get("detail"):
                item["detail"] = self._humanize_fields(
                    str(item["detail"]), field_map,
                )
            out.append(item)
        return out

    def _humanize_fields(self, text: str, field_map: list[dict] | None) -> str:
        for item in field_map or []:
            label = str(item.get("label") or "")
            technical = str(item.get("technical") or "")
            if not label:
                continue
            if technical and len(technical) > 4 and technical != label:
                text = re.sub(
                    rf"(?<![\w]){re.escape(technical)}(?![\w])", label, text,
                )
            field = str(item.get("field") or "")
            if field:
                text = text.replace(field, label)
        return text

    def _composer_context(self, state: SessionState) -> dict:
        plan = state.plan
        metric = self._metric_for_plan(plan)
        ctx = project_for_composer(state)
        field_map = self._field_map(plan)
        metric_name = metric.name if metric else plan.metric
        if plan is not None and plan.entity_count is not None:
            metric_name = plan.entity_count.entity
            if not metric_name.endswith("数"):
                metric_name += "数"
        ctx.update({
            "metric_name": metric_name,
            "metric_unit": metric.raw.get("unit") if metric else None,
            "metric_type": metric.type if metric else "aggregate",
            "time_axis": metric.raw.get("time_axis") if metric else None,
            "definition": metric.raw.get("definition") if metric else None,
            "memory": state.memory or {},
            "dimension_labels": [self.sem.dimension(d).raw.get("name_zh", d)
                                 for d in (plan.dimensions if plan else [])],
            "filters_label": "、".join(
                f"{self.sem.dimension(b['dim']).raw.get('name_zh', b['dim'])}="
                + _short_values(b.get("values", []))
                for b in state.bindings
            ),
            "compare": bool(plan and (plan.compare or plan.comparison)),
            "comparison": plan.comparison.model_dump(mode="json") if plan and plan.comparison else None,
            "degraded_reason": plan.degraded_reason if plan else None,
            "checks": self._humanize_checks(state.checks, field_map),
            "facts": state.facts,
            "field_map": field_map,
        })
        return ctx

    def _plan_filters_label(self, plan: QueryPlan) -> str:
        from app.core.context import humanize_slots
        raw = {"filters": {d: spec.model_dump() for d, spec in plan.filters.items()}}
        shown = humanize_slots(raw, self.dim_values_path).get("filters", {})
        parts = []
        for dim, spec in shown.items():
            value = spec.get("value") if isinstance(spec, dict) else spec
            values = value if isinstance(value, list) else [value]
            name = self.sem.dimension(dim).raw.get("name_zh", dim)
            parts.append(f"{name}={_short_values(values)}")
        return "、".join(parts)


def _short_values(values: list, head: int = 3) -> str:
    shown = "/".join(map(str, values[:head]))
    if len(values) > head:
        shown += f" 等{len(values)}个"
    return shown
