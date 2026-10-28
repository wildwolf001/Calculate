"""The driving loop: state machine owns transitions, model is a subroutine.

Every step appends exactly one event. The loop re-reads the event log each
iteration (`reduce`), so an in-memory object can never drift from history.
"""
from __future__ import annotations

import traceback
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
from app.core.state import SessionState, Slots, Status
from app.core.trace import Tracer, new_trace_id
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
    plan_from_slots,
)
from app.domain.plan import FilterSpec, QueryPlan
from app.domain import lookup as lookup_mod
from app.domain.presentation import display_spec
from app.domain.repair import build_repair, repair_text
from app.domain.rewrite import rewrite
from app.domain.semantic.loader import SemanticLayer, SemanticError
from app.llm.rule_parser import RuleParser
from app.llm.analyze import LLMAnalysisWriter

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
                 ctx=None, analysis_writer: LLMAnalysisWriter | None = None):
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
        self._results: dict[tuple[str, int], object] = {}

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
        """One retry with an llm.retry event; re-raises on second failure."""
        try:
            return fn()
        except Exception as exc:
            if state.policy.can_retry(stage):
                events.append(("llm.retry", {"stage": stage, "attempt": 1, "reason": str(exc)}))
                return fn()
            raise

    # ---------- handlers ----------
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

        apply_context_action(result, conversation_context)

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
            message = result.explanation or "这个问题暂时不支持。"
            repair = build_repair(self.sem, self._repair_slots(state, result.slots))
            if repair["options"]:
                extra = []
                if repair["clarification"]:
                    extra.append(repair["clarification"])
                extra.append("可以这样问：\n" + "\n".join(f"- {q}" for q in repair["options"]))
                message = f"{message}\n\n" + "\n\n".join(extra)
            elif self._suggestions() not in message:
                message = f"{message}\n\n{self._suggestions()}"
            events.append(("answer.composed", {
                "text": message, "result_id": None, "options": repair["options"],
            }))
            events.append(("result.presented", {"result_id": None}))
            return events

        if result.unresolved:
            repair = build_repair(
                self.sem, self._repair_slots(state, result.slots), unknown=result.unresolved
            )
            message = (
                f"没有识别出「{result.unresolved[0]}」对应的指标或维度。\n\n"
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
                "notes": result.notes,
                "interpretations": meta,
                "preferred_interpretation": preferred,
                "ambiguity_note": result.ambiguity_note,
            }))
            return events + self._multi_interpretation_events(state, result)

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

    def _multi_question_events(self, state: SessionState,
                               result) -> list[tuple[str, dict]]:
        """Execute independent sub-questions in order with failure isolation."""
        events: list[tuple[str, dict]] = []
        blocks: list[str] = []
        executed: list[dict] = []
        non_sql: list[dict] = []
        failures: list[dict] = []
        previous_slots: dict | None = None
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
                blocks.append(f"**{index}. {question}**\n\n{text}")
                continue
            if intent_type in {"unsupported", "smalltalk", "analysis"}:
                text = sub.get("explanation") or "这个问题暂时无法生成可执行查询。"
                non_sql.append({"id": sid, "question": question, "text": text})
                blocks.append(f"**{index}. {question}**\n\n{text}")
                continue

            slots_payload = dict(sub.get("slots") or {})
            if intent_type == "lookup":
                dim = sub.get("lookup_dimension") or sub.get("list_dim")
                slots_payload["intent"] = "lookup"
                slots_payload["dimensions"] = [dim] if dim else []
            if (
                previous_slots
                and sub.get("context_action") in {"inherit", "refine"}
            ):
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

            try:
                sub_events, entry = self._run_subquestion(
                    state, sid, question, slots_payload,
                    preferred=not executed,
                )
            except Exception as exc:
                reason = str(exc)
                failures.append({"id": sid, "question": question, "reason": reason})
                events.append(("subquestion.rejected", {
                    "subquestion_id": sid,
                    "status": "failed",
                    "reason": reason,
                }))
                blocks.append(f"**{index}. {question}**\n\n这个问题暂时无法回答：{reason}")
                continue

            events.extend(sub_events)
            previous_slots = dict(slots_payload)
            executed.append(entry)
            context = self._entry_context(entry)
            from app.llm.compose_answer import TemplateComposer
            text = TemplateComposer().compose(context)
            blocks.append(f"**{index}. {question}**\n\n{text}")

        if not executed and not non_sql:
            return events + [("error.raised", {
                "code": "NO_SUBQUESTION",
                "message": "没有可执行的子问题。请把问题拆成更明确的指标或时间。",
                "recoverable": False,
            })]

        header = f"你这次一共问了 {len(questions)} 个问题，结果如下："
        if len(result.subquestions) > MAX_SUBQUESTIONS:
            header += f"（单次最多处理 {MAX_SUBQUESTIONS} 个）"
        text = self._with_draft_notice(header + "\n\n" + "\n\n".join(blocks))
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

    def _run_subquestion(self, state: SessionState, ident: str, question: str,
                         slots_payload: dict, *, preferred: bool,
                         analysis_section: str | None = None,
                         analysis_item: str | None = None
                         ) -> tuple[list[tuple[str, dict]], dict]:
        events: list[tuple[str, dict]] = []
        try:
            slots = Slots.model_validate(slots_payload)
        except Exception as exc:
            raise SemanticError(f"槽位非法：{exc}") from exc
        if slots.unresolved:
            raise SemanticError("没有识别出「" + "、".join(slots.unresolved) + "」")

        parser_context = project_for_parser(
            state, dim_values_path=self.dim_values_path
        )
        grounded = self.grounder.ground(
            slots.model_dump(mode="json"), slots.unresolved, parser_context
        )
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

        try:
            if slots.intent == "lookup":
                dim = slots.dimensions[0] if slots.dimensions else None
                plan = member_list_plan(dim, slots, self.sem)
            else:
                plan = plan_from_slots(slots, self.sem)
            plan.interpretation_id = ident
            plan.interpretation_label = question[:32]
            plan.preferred = preferred
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
        payload.update({
            "question": question,
            "display": display_spec(plan, self.sem, artifact),
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
        events.append(("sql.executed", payload))

        issues_checked, facts = check(
            plan, self.sem, artifact,
            db_path=self.warehouse, quality=self.quality,
        )
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

    def _entry_context(self, entry: dict) -> dict:
        plan, artifact = entry["plan"], entry["artifact"]
        metric = self.sem.metric(plan.metric) if plan.metric else None
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
            "dimension_labels": [
                self.sem.dimension(dim).raw.get("name_zh", dim)
                for dim in plan.dimensions
            ],
            "rows": artifact.rows,
            "columns": artifact.columns,
            "row_count": artifact.row_count,
            "truncated": artifact.truncated,
            "time": time,
            "comparison": comparison,
            "compare": bool(comparison or plan.compare),
            "filters_label": self._plan_filters_label(plan),
            "checks": entry["issues"],
            "facts": entry["facts"],
            "meta": artifact.meta,
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
            "failures": failures,
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
                metric = self.sem.metric(plan.metric) if plan.metric else None
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
                lines.append(f"- {failure['title']}：{failure['reason']}")
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
        filter_failure: tuple[dict, str] | None = None
        question = self._last_user_text(state)

        for interp in interps:
            iid = interp["id"]
            is_pref = iid == preferred_id
            try:
                slots = Slots.model_validate(interp["slots"])
                if not slots.visualization and result.visualization:
                    slots.visualization = result.visualization
                if slots.unresolved:
                    raise SemanticError(f"未绑定词: {', '.join(slots.unresolved)}")
                grounded = self.grounder.ground(
                    slots.model_dump(mode="json"), slots.unresolved, project_for_parser(state, dim_values_path=self.dim_values_path)
                )
                if grounded.ambiguity:
                    raise SemanticError(grounded.ambiguity.get("message", "值绑定失败"))
                for binding in grounded.bindings:
                    slots.filters[binding["dim"]] = FilterSpec(
                        op=binding.get("op", "in"), value=binding["ids"]
                    )
                plan = plan_from_slots(slots, self.sem)
                plan.interpretation_id = iid
                plan.interpretation_label = interp.get("label")
                plan.preferred = is_pref
            except Exception as exc:
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
            payload.update({
                "question": question,
                "display": display_spec(plan, self.sem, artifact),
                "interpretation_id": iid,
                "interpretation_label": interp.get("label"),
                "preferred": is_pref,
                "plan": plan.model_dump(mode="json"),
            })
            events.append(("sql.executed", payload))
            issues, facts = check(plan, self.sem, artifact,
                                  db_path=self.warehouse, quality=self.quality)
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
            events.append(("error.raised", {
                "code": "NO_INTERPRETATION",
                "message": "这几种理解都无法生成可执行的查询。",
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
            metric = self.sem.metric(plan.metric)
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
            return self._call_with_retry(
                "compose_answer", lambda: self.composer.compose(ctx), state, events
            )
        except Exception as exc:
            from app.llm.compose_answer import TemplateComposer
            log_event(logger, "composer fallback", error=str(exc))
            return TemplateComposer().compose(ctx)

    def _handle_grounding(self, state: SessionState) -> list[tuple[str, dict]]:
        ctx = project_for_parser(state, dim_values_path=self.dim_values_path)
        grounded = self.grounder.ground(
            state.slots.model_dump(mode="json"), state.slots.unresolved, ctx
        )
        if grounded.ambiguity:
            payload = dict(grounded.ambiguity)
            payload["partial_slots"] = state.slots.model_dump(mode="json")
            return [("ambiguity.detected", payload)]
        return [("values.grounded", {
            "bindings": grounded.bindings,
            "notes": grounded.notes,
        })]

    def _handle_planning(self, state: SessionState) -> list[tuple[str, dict]]:
        try:
            slots = Slots.model_validate(state.slots.model_dump())
            if slots.intent == "lookup":
                plan = member_list_plan(
                    slots.dimensions[0] if slots.dimensions else None, slots, self.sem
                )
            else:
                plan = plan_from_slots(slots, self.sem)
        except SemanticError as exc:
            message = f"{exc}\n\n{self._suggestions()}"
            return [
                ("plan.rejected", {"reason": str(exc), "issues": [], "recoverable": False}),
                ("error.raised", {
                    "code": "PLAN_UNSUPPORTED", "message": message, "recoverable": False,
                }),
            ]
        return [("plan.compiled", {"plan": plan.model_dump(mode="json")})]

    def _handle_validating(self, state: SessionState) -> list[tuple[str, dict]]:
        plan = QueryPlan.model_validate(state.plan.model_dump())
        try:
            compiled, issues = prepare(plan, self.sem)
        except PipelineError as exc:
            filter_issue = next(
                (i for i in exc.issues if i.code == "UNSUPPORTED_FILTER"), None
            )
            if filter_issue:
                repair = build_repair(
                    self.sem, self._repair_slots(state, state.slots.model_dump(mode="json"))
                )
                message = f"{filter_issue.message}\n\n{repair_text(repair)}"
                return [
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
                    return [
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
            return [
                ("plan.rejected", {
                    "reason": str(exc),
                    "issues": [i.as_dict() for i in exc.issues],
                    "recoverable": False,
                }),
                ("error.raised", {
                    "code": "PLAN_INVALID", "message": str(exc), "recoverable": False,
                }),
            ]
        return [("sql.compiled", self._compiled_payload(compiled, issues))]

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
            return [("error.raised", {"code": "PLAN_REJECTED", "message": str(exc),
                                      "recoverable": False})]
        except Exception as exc:
            log_event(logger, "execution.error", error=f"{type(exc).__name__}: {exc}",
                      detail=traceback.format_exc()[:4000])
            return [("error.raised", {
                "code": "EXECUTION_FAILED",
                "message": "查询执行失败。可以缩小时间范围或减少维度后重试。",
                "recoverable": False,
                "hint": "可以缩小时间范围或减少维度后重试。",
            })]
        result.fingerprint = compiled.fingerprint(
            int(self.sem.raw.get("compiler_version", 1)),
            self.sem.version,
        )
        result.result_id = f"r{state.turn}-{result.fingerprint}"
        self._results[(state.session_id, state.turn)] = result
        payload = result.as_dict()
        payload["question"] = self._last_user_text(state)
        payload["display"] = display_spec(plan, self.sem, result)
        payload["plan"] = plan.model_dump(mode="json")
        return [("sql.executed", payload)]

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
                              db_path=self.warehouse, quality=self.quality)
        payload = {
            "result_id": result.result_id,
            "issues": [i.as_dict() for i in issues],
            "facts": facts,
            "blocked": any(i.severity == "error" for i in issues),
        }
        events: list[tuple[str, dict]] = [("checks.completed", payload)]
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

    def _composer_context(self, state: SessionState) -> dict:
        plan = state.plan
        metric = self.sem.metric(plan.metric) if plan else None
        ctx = project_for_composer(state)
        ctx.update({
            "metric_name": metric.name if metric else plan.metric,
            "metric_unit": metric.raw.get("unit") if metric else None,
            "metric_type": metric.type if metric else "aggregate",
            "definition": metric.raw.get("definition") if metric else None,
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
            "checks": state.checks,
            "facts": state.facts,
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
