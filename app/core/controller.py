"""The driving loop: state machine owns transitions, model is a subroutine.

Every step appends exactly one event. The loop re-reads the event log each
iteration (`reduce`), so an in-memory object can never drift from history.
"""
from __future__ import annotations

import re
import traceback
from typing import AsyncIterator

from app.config.settings import MAX_STEPS, MAX_SUBQUESTIONS
from app.core.context import project_for_composer, project_for_parser
from app.core.event_store import EventStore
from app.core.events import Event
from app.core.interfaces import AnswerComposer, IntentParser, ValueGrounder
from app.core.logging import log_event, logger
from app.core.state import SessionState, Slots, Status
from app.core.trace import Tracer, new_trace_id
from app.domain.analysis import (
    DEFAULT_MODULES,
    MODULE_IDS,
    build_analysis_items,
    module_catalog,
)
from app.domain.insights import build_insights
from app.domain.pipeline import (
    PipelineError,
    check,
    execute_prepared,
    prepare,
)
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
from app.llm.analyze import LLMAnalysisWriter
from app.llm.rule_parser import RuleParser

SMALLTALK_TEXT = (
    "你好，我是明账。直接问经营问题就行，比如「4月销售额最高的品类」、"
    "「5月延迟率最高的商家」或「有多少复购客户」。"
)
UNSUPPORTED_SUGGESTIONS = (
    "可以问：商品销售额 / 订单数 / 支付金额 / 延迟率 / 平均评分 / 低分率 / "
    "复购客户数 / 客单价，并指定时间（如「2025年5月」）和维度（如「按品类」）。"
)
# A short, general request routes to the deterministic analysis report. Keep it
# strict: "4月经营分析" has a period and should stay a normal query.
ANALYSIS_CUE = re.compile(r"(经营分析|经营情况|整体分析|生意分析|经营诊断)")

class SubquestionError(RuntimeError):
    """A single sub-question failed; the remaining ones still run."""


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
                 analysis_writer: LLMAnalysisWriter | None = None):
        self.store = store
        self.sem = sem
        self.parser = parser
        self.grounder = grounder
        self.composer = composer
        self.tracer = tracer
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
        """Run the deterministic analysis pipeline with user-selected scope.

        Unlike the natural-language cue path, this bypasses intent parsing: the
        dialog already told us which preset modules and custom block to run.
        """
        state = self.store.replay(session_id)
        turn = state.turn + 1
        trace_id = new_trace_id()
        yield self.store.append(session_id, "turn.started",
                                {"turn": turn, "kind": "analysis"},
                                trace_id=trace_id)
        yield self.store.append(session_id, "user.message", {
            "text": self._analysis_request_text(options),
            "turn": turn,
            "kind": "analysis",
        }, trace_id=trace_id)
        state.turn = turn
        try:
            analysis_events = self._analysis_events(state, options)
        except Exception as exc:
            log_event(logger, "analysis.failed",
                      error=f"{type(exc).__name__}: {exc}",
                      detail=traceback.format_exc()[:4000])
            analysis_events = [("error.raised", {
                "code": "ANALYSIS_FAILED",
                "message": "经营分析生成失败。可以缩小分析范围或稍后重试。",
                "recoverable": False,
            })]
        for ev_type, payload in analysis_events:
            yield self.store.append(session_id, ev_type, payload, trace_id=trace_id)
        final = self.store.replay(session_id)
        yield self.store.append(session_id, "turn.completed",
                                {"status": final.status.value, "turn": turn},
                                trace_id=trace_id)

    def _analysis_request_text(self, options: dict | None) -> str:
        options = options or {}
        catalog = {m["id"]: m for m in module_catalog()}
        labels = [
            catalog[mid]["label"] for mid in (options.get("sections") or [])
            if mid in catalog
        ]
        text = "生成经营分析"
        if labels:
            text += "：" + "、".join(labels)
        custom = (options.get("custom_text") or "").strip()
        if custom:
            text += "\n自定义分析：" + custom
        return text

    # ---------- helpers ----------
    def _span(self, name: str, trace_id: str, state: SessionState):
        if self.tracer is None:
            from contextlib import nullcontext
            return nullcontext()
        return self.tracer.span(f"controller.{name.lower()}", trace_id,
                                session_id=state.session_id, turn=state.turn, status=state.status.value)

    def _fingerprint(self, compiled) -> str:
        """Fingerprint tied to both semantic and compiler versions."""
        return compiled.fingerprint(
            compiler_version=int(self.sem.raw.get("compiler_version", 1)),
            semantic_version=int(self.sem.version),
        )

    def _last_user_text(self, state: SessionState) -> str:
        for m in reversed(state.messages):
            if m["role"] == "user":
                return m["text"]
        return ""

    def _repair_slots(self, state: SessionState, slots: dict | None) -> dict:
        """Fill entity/time for repair hints, preferring display names."""
        data = dict(slots or {})
        try:
            ctx = RuleParser(self.sem).extract_context(self._last_user_text(state))
        except Exception:
            ctx = {}
        if ctx.get("filters"):
            data["filters"] = ctx["filters"]
        elif data.get("filters"):
            from app.core.context import humanize_slots
            data = humanize_slots(data)
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
        events: list[tuple[str, dict]] = [(
            "query.normalized",
            {
                "original": rw.original,
                "normalized": rw.normalized,
                "changes": rw.changes,
                "kind": rw.kind,
                "followup": rw.followup,
            },
        )]

        if self._wants_analysis(rw.normalized):
            events.append(("intent.classified", {
                "intent_type": "analysis", "tier": "B", "confidence": 1.0,
                "rewritten_question": rw.normalized,
            }))
            return events + self._analysis_events(state)

        if rw.kind == "smalltalk":
            events.append(("answer.composed", {"text": SMALLTALK_TEXT, "result_id": None}))
            events.append(("result.presented", {"result_id": None}))
            return events
        if rw.kind == "lookup":
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

        ctx = project_for_parser(state)
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

        events.append(("intent.classified", {
            "intent_type": result.intent_type, "tier": result.tier,
            "confidence": result.confidence,
            "rewritten_question": result.rewritten_question,
        }))

        if result.intent_type == "analysis":
            return events + self._analysis_events(state)

        # A compound message is dispatched before the single-intent branches:
        # the top-level intent describes the container, and one sub-question
        # may legitimately be "unsupported" while the others are queries.
        if len(result.subquestions) >= 2 and not result.interpretations:
            events.append(("subquestions.planned", self._subquestions_payload(result)))
            return events + self._multi_question_events(state, result)

        if result.intent_type == "smalltalk":
            events.append(("answer.composed", {"text": SMALLTALK_TEXT, "result_id": None}))
            events.append(("result.presented", {"result_id": None}))
            return events

        if result.intent_type == "lookup":
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
            elif UNSUPPORTED_SUGGESTIONS not in message:
                message = f"{message}\n\n{UNSUPPORTED_SUGGESTIONS}"
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
            events.append(("subquestions.planned", self._subquestions_payload(result)))
            sub = result.subquestions[0]
            if sub.get("slots"):
                slots = dict(sub["slots"])
                if sub.get("intent_type") == "lookup":
                    slots["intent"] = "lookup"
                    if sub.get("lookup_dimension"):
                        slots["dimensions"] = [sub["lookup_dimension"]]
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

    # ---------- multi-question turn ----------
    def _multi_question_events(self, state: SessionState, result) -> list[tuple[str, dict]]:
        """Execute every sub-question in one message, in the order asked.

        Each sub-question gets its own slots -> ground -> plan -> SQL -> checks
        cycle. Failures stay local: one rejected sub-question never cancels the
        others, and the turn always ends with one merged answer.
        """
        base_ctx = project_for_parser(state)
        answers: list[dict] = []
        executed: list[dict] = []
        events: list[tuple[str, dict]] = []
        primary_slots: dict | None = None
        prev_slots: dict | None = None

        for idx, sub in enumerate(result.subquestions[:MAX_SUBQUESTIONS], 1):
            sid = f"sq{idx}"
            question = str(sub.get("question") or "").strip() or f"第 {idx} 个问题"
            intent_type = sub.get("intent_type") or "query"
            slots_payload = dict(sub.get("slots") or {})
            explanation = sub.get("explanation")
            lookup_dim = sub.get("lookup_dimension")

            if intent_type == "query" and not self._slots_payload_has_content(slots_payload):
                reparsed = self._reparse_subquestion(question, base_ctx, prev_slots, state)
                if reparsed is not None:
                    intent_type = reparsed.intent_type
                    slots_payload = dict(reparsed.slots or {})
                    explanation = reparsed.explanation or explanation
                    lookup_dim = reparsed.lookup_dim or lookup_dim

            events.append(("subquestion.started", {
                "subquestion_id": sid, "index": idx,
                "question": question, "intent_type": intent_type,
            }))

            if intent_type in {"smalltalk", "explain", "unsupported"}:
                text = explanation or (
                    "这个问题暂时不支持。" if intent_type == "unsupported"
                    else "这条信息不在当前分析范围内。"
                )
                answers.append(self._answer_entry(sid, question, text, intent_type, None))
                events.append(("subquestion.answered", {
                    "subquestion_id": sid, "status": intent_type, "text": text,
                }))
                if slots_payload:
                    prev_slots = slots_payload
                continue

            if intent_type == "lookup":
                slots_payload = dict(slots_payload)
                slots_payload["intent"] = "lookup"
                dim = lookup_dim or ((slots_payload.get("dimensions") or [None])[0])
                if not dim:
                    text = "没有识别出要列举的维度。"
                    answers.append(self._answer_entry(sid, question, text, "failed", None))
                    events.append(("subquestion.rejected", {
                        "subquestion_id": sid, "status": "failed", "reason": text,
                    }))
                    continue
                slots_payload["dimensions"] = [dim]

            try:
                sub_events, entry = self._run_subquestion(
                    state, sid, question, slots_payload, base_ctx,
                    is_preferred=(not executed),
                )
            except SubquestionError as exc:
                text = f"这个问题暂时无法回答：{exc}"
                answers.append(self._answer_entry(sid, question, text, "failed", None))
                events.append(("subquestion.rejected", {
                    "subquestion_id": sid, "status": "failed", "reason": str(exc),
                }))
                continue

            events.extend(sub_events)
            executed.append(entry)
            answers.append(self._answer_entry(sid, question, None, "query", entry))
            if primary_slots is None:
                primary_slots = slots_payload
            prev_slots = slots_payload

        for ans in answers:
            if ans.get("entry") is None:
                continue
            try:
                ans["text"] = self._compose_subquestion(state, ans["entry"], executed)
            except Exception as exc:  # never lose the whole turn to one bad composition
                log_event(logger, "subquestion.compose_failed",
                          subquestion_id=ans["id"],
                          error=f"{type(exc).__name__}: {exc}")
                ans["text"] = "（这一问的结论生成失败，可查看右侧查询结果。）"

        if not answers:
            events.append(("error.raised", {
                "code": "NO_SUBQUESTION",
                "message": "没有可执行的子问题。",
                "recoverable": False,
            }))
            return events

        if len(answers) == 1:
            text = answers[0]["text"]
        else:
            blocks = [
                f"**{i}. {a['question']}**\n\n{a['text']}"
                for i, a in enumerate(answers, 1)
            ]
            header = f"你这次一共问了 {len(answers)} 个问题，逐条回答："
            if len(result.subquestions) > MAX_SUBQUESTIONS:
                header += (f"（一次最多处理 {MAX_SUBQUESTIONS} 个，"
                           "其余问题请下一轮再问）")
            text = header + "\n\n" + "\n\n".join(blocks)

        combined: list[str] = []
        seen: set[str] = set()
        for entry in executed:
            artifact = entry["artifact"]
            for insight in build_insights(
                entry["plan"], artifact.columns, artifact.rows, artifact.row_count,
                artifact.truncated, entry["issues"], self.sem,
            ):
                if insight not in seen:
                    seen.add(insight)
                    combined.append(insight)
        combined = combined[:4]
        text += self._insight_block(combined)

        primary_rid = executed[0]["artifact"].result_id if executed else None
        events.append(("answer.composed", {
            "text": text,
            "result_id": primary_rid,
            "insights": combined,
            "subquestions": [
                {
                    "id": a["id"], "question": a["question"], "status": a["status"],
                    "result_id": a["entry"]["artifact"].result_id if a.get("entry") else None,
                }
                for a in answers
            ],
        }))
        events.append(("result.presented", {"result_id": primary_rid}))
        if primary_slots is not None:
            # The first executed sub-question is the turn's observable context,
            # so "那 5 月呢？" in the next turn inherits something meaningful.
            events.append(("subquestions.completed", {"primary_slots": primary_slots}))
        return events

    @staticmethod
    def _subquestions_payload(result) -> dict:
        return {
            "questions": [q.get("question", "") for q in result.subquestions],
            "items": [
                {"id": f"sq{i}", "question": q.get("question", ""),
                 "intent_type": q.get("intent_type", "query")}
                for i, q in enumerate(result.subquestions, 1)
            ],
            "depth": 1,
        }

    @staticmethod
    def _answer_entry(sid: str, question: str, text: str | None, status: str,
                      entry: dict | None) -> dict:
        return {"id": sid, "question": question, "text": text,
                "status": status, "entry": entry}

    @staticmethod
    def _slots_payload_has_content(payload: dict) -> bool:
        return bool(payload.get("metric") or payload.get("dimensions")
                    or payload.get("filters") or payload.get("time")
                    or payload.get("comparison"))

    @staticmethod
    def _short_label(text: str, limit: int = 24) -> str:
        """Compact label for the result tabs, keeping the full question in prose."""
        compact = " ".join(text.split())
        return compact if len(compact) <= limit else compact[:limit] + "…"

    def _reparse_subquestion(self, question: str, base_ctx: dict,
                             prev_slots: dict | None, state: SessionState):
        """Last-resort parse when the model split a question but left slots empty."""
        try:
            ctx = dict(base_ctx)
            if prev_slots:
                from app.core.context import humanize_slots
                ctx["slots"] = humanize_slots(prev_slots)
            parsed = self.parser.parse(question, ctx)
        except Exception as exc:
            log_event(logger, "subquestion.reparse_failed", question=question,
                      error=f"{type(exc).__name__}: {exc}")
            return None
        if parsed.subquestions:
            return None  # never recurse into another split
        if parsed.intent_type == "query" and prev_slots:
            # Dependent sub-question ("那同比呢？"): inherit only what it omitted.
            for key in ("metric", "time", "comparison"):
                if not parsed.slots.get(key) and prev_slots.get(key):
                    parsed.slots[key] = prev_slots[key]
        return parsed

    def _run_subquestion(self, state: SessionState, sid: str, question: str,
                         slots_payload: dict, base_ctx: dict,
                         is_preferred: bool) -> tuple[list[tuple[str, dict]], dict]:
        try:
            slots = Slots.model_validate(slots_payload)
        except Exception as exc:
            raise SubquestionError(f"槽位非法：{exc}") from exc
        if slots.unresolved:
            raise SubquestionError("没有识别出「" + "、".join(slots.unresolved) + "」")

        grounded = self.grounder.ground(
            slots.model_dump(mode="json"), slots.unresolved, base_ctx
        )
        if grounded.ambiguity:
            raise SubquestionError(grounded.ambiguity.get("message") or "值绑定失败")
        for binding in grounded.bindings:
            slots.filters[binding["dim"]] = FilterSpec(
                op=binding.get("op", "in"), value=binding["ids"]
            )

        events: list[tuple[str, dict]] = []
        try:
            if slots.intent == "lookup":
                dim = slots.dimensions[0] if slots.dimensions else None
                plan = member_list_plan(dim, slots, self.sem)
            else:
                plan = plan_from_slots(slots, self.sem)
        except Exception as exc:
            raise SubquestionError(str(exc)) from exc

        label = self._short_label(question)
        plan.interpretation_id = sid
        plan.interpretation_label = label
        plan.preferred = is_preferred

        try:
            compiled, issues = prepare(plan, self.sem)
        except PipelineError as exc:
            degraded = None
            if any(i.code == "UNSUPPORTED_GRAIN" for i in exc.issues):
                degraded = degrade_unsupported_dimensions(plan, self.sem)
            if not degraded:
                raise SubquestionError(str(exc)) from exc
            new_plan, removed = degraded
            try:
                compiled, issues = prepare(new_plan, self.sem)
            except PipelineError:
                raise SubquestionError(str(exc)) from exc
            plan = new_plan
            plan.interpretation_id = sid
            plan.interpretation_label = label
            plan.preferred = is_preferred
            events.append(("plan.degraded", {
                "subquestion_id": sid,
                "reason": plan.degraded_reason,
                "removed_dimensions": removed,
            }))

        events.append(("plan.compiled", {
            "plan": plan.model_dump(mode="json"),
            "interpretation_id": sid,
            "interpretation_label": label,
            "preferred": is_preferred,
            "subquestion_id": sid,
        }))
        events.append(("sql.compiled", {
            "sql": compiled.sql,
            "params": [str(p) for p in compiled.params],
            "fingerprint": self._fingerprint(compiled),
            "columns": compiled.columns,
            "tables_used": compiled.tables_used,
            "meta": compiled.meta,
            "issues": [i.as_dict() for i in issues if i.severity != "error"],
            "interpretation_id": sid,
            "preferred": is_preferred,
            "subquestion_id": sid,
        }))

        try:
            artifact = execute_prepared(compiled)
        except Exception as exc:
            log_event(logger, "subquestion.execution_failed", subquestion_id=sid,
                      error=f"{type(exc).__name__}: {exc}")
            raise SubquestionError("查询执行失败，可缩小时间或维度范围后重试。") from exc

        artifact.fingerprint = self._fingerprint(compiled)
        artifact.result_id = f"r{state.turn}-{sid}-{artifact.fingerprint}"
        self._results[(state.session_id, state.turn, sid)] = artifact
        payload = artifact.as_dict()
        payload.update({
            "question": question,
            "display": display_spec(plan, self.sem, artifact),
            "interpretation_id": sid,
            "interpretation_label": label,
            "preferred": is_preferred,
            "plan": plan.model_dump(mode="json"),
            "subquestion_id": sid,
        })
        events.append(("sql.executed", payload))

        check_issues, facts = check(plan, self.sem, artifact)
        issue_dicts = [i.as_dict() for i in check_issues]
        events.append(("checks.completed", {
            "result_id": artifact.result_id,
            "issues": issue_dicts,
            "facts": facts,
            "blocked": any(i.severity == "error" for i in check_issues),
            "interpretation_id": sid,
            "interpretation_label": label,
            "preferred": is_preferred,
            "subquestion_id": sid,
        }))
        entry = {
            "id": sid, "question": question, "plan": plan,
            "artifact": artifact, "issues": issue_dicts, "facts": facts,
        }
        return events, entry

    def _compose_subquestion(self, state: SessionState, entry: dict,
                             executed: list[dict] | None = None) -> str:
        plan, artifact = entry["plan"], entry["artifact"]
        if plan.kind == "member_list":
            text = lookup_mod.describe(plan.list_dim or "", artifact.rows, len(artifact.rows))
            scope: list[str] = []
            if plan.filters:
                scope.append(self._plan_filters_label(plan))
            if plan.time:
                scope.append(f"{plan.time.start} 至 {plan.time.end}")
            return f"限定范围：{'；'.join(scope)}。\n\n{text}" if scope else text
        ctx = self._subquestion_composer_context(state, entry)
        ctx["original_question"] = self._last_user_text(state)
        ctx["siblings"] = [
            self._sibling_digest(other)
            for other in (executed or [])
            if other is not entry
        ]
        return self._compose(ctx, state)

    def _sibling_digest(self, entry: dict) -> dict:
        """Compact facts for another sub-question in the same turn."""
        plan, artifact = entry["plan"], entry["artifact"]
        metric = self.sem.metric(plan.metric) if plan.metric else None
        return {
            "question": entry["question"],
            "metric": plan.metric,
            "metric_name": metric.name if metric else (plan.list_dim or "结果"),
            "metric_unit": metric.raw.get("unit") if metric else None,
            "dimensions": plan.dimensions,
            "time": plan.time.model_dump(mode="json") if plan.time else None,
            "comparison": plan.comparison.model_dump(mode="json") if plan.comparison else None,
            "filters": {d: s.model_dump(mode="json") for d, s in plan.filters.items()},
            "filters_label": self._plan_filters_label(plan),
            "columns": artifact.columns,
            "rows": artifact.rows[:20],
            "row_count": artifact.row_count,
            "checks": entry["issues"],
            "facts": entry["facts"],
        }

    def _subquestion_composer_context(self, state: SessionState, entry: dict) -> dict:
        plan, artifact = entry["plan"], entry["artifact"]
        metric = self.sem.metric(plan.metric) if plan.metric else None
        return {
            "session_id": state.session_id,
            "question": entry["question"],
            "metric": plan.metric,
            "metric_name": metric.name if metric else (plan.list_dim or "结果"),
            "metric_unit": metric.raw.get("unit") if metric else None,
            "metric_type": metric.type if metric else "aggregate",
            "definition": metric.raw.get("definition") if metric else None,
            "dimensions": plan.dimensions,
            "dimension_labels": [
                self.sem.dimension(d).raw.get("name_zh", d) for d in plan.dimensions
            ],
            "time": plan.time.model_dump(mode="json") if plan.time else None,
            "compare": bool(plan.compare or plan.comparison),
            "comparison": plan.comparison.model_dump(mode="json") if plan.comparison else None,
            "filters": {d: s.model_dump(mode="json") for d, s in plan.filters.items()},
            "filters_label": self._plan_filters_label(plan),
            "columns": artifact.columns,
            "rows": artifact.rows,
            "row_count": artifact.row_count,
            "truncated": artifact.truncated,
            "sql": artifact.sql,
            "checks": entry["issues"],
            "facts": entry["facts"],
            "degraded_reason": plan.degraded_reason,
            "notes": plan.notes,
        }

    # ---------- on-demand business analysis ----------
    @staticmethod
    def _wants_analysis(text: str) -> bool:
        t = (text or "").strip()
        return bool(ANALYSIS_CUE.search(t)) and len(t) <= 30 and not re.search(r"\d", t)

    def _analysis_focus(self, state: SessionState) -> dict:
        """Use the most recent executed plan as the session's analysis focus."""
        last = None
        for payload in state.artifacts.values():
            if isinstance(payload, dict) and payload.get("plan"):
                last = payload
        if not last:
            return {"filters": {}, "anchor_end": None, "reference_count": 0,
                    "last_question": None, "focus_label": ""}
        plan_payload = last.get("plan") or {}
        filters = {
            dim: spec for dim, spec in (plan_payload.get("filters") or {}).items()
            if isinstance(spec, dict) and spec.get("value") is not None
        }
        anchor = None
        comp = plan_payload.get("comparison") or {}
        if (comp.get("current") or {}).get("end"):
            anchor = comp["current"]["end"]
        elif (plan_payload.get("time") or {}).get("end"):
            anchor = plan_payload["time"]["end"]
        focus_label = ""
        try:
            focus_label = self._plan_filters_label(QueryPlan.model_validate(plan_payload))
        except Exception:
            focus_label = ""
        return {
            "filters": filters,
            "anchor_end": anchor,
            "reference_count": len(state.artifacts),
            "last_question": last.get("question"),
            "focus_label": focus_label,
        }

    @staticmethod
    def _analysis_value_hint() -> str:
        """Actionable hint when a custom entity cannot be resolved."""
        import json
        from app.config.settings import DIM_VALUES_PATH

        try:
            data = json.loads(DIM_VALUES_PATH.read_text(encoding="utf-8"))
            values = (data.get("values") or {}).get(
                "categories.category_name_zh"
            ) or []
            return "、".join(map(str, values[:8]))
        except Exception:
            return ""

    def _analysis_events(self, state: SessionState,
                         options: dict | None = None) -> list[tuple[str, dict]]:
        options = options or {}
        focus = self._analysis_focus(state)
        raw_sections = options.get("sections") or []
        custom_text = (options.get("custom_text") or "").strip()[:500]
        custom_title = (options.get("custom_title") or "").strip()[:60]

        valid_sections = [m for m in raw_sections if m in MODULE_IDS]
        if not raw_sections and not custom_text:
            selected_modules = None          # default full report
        elif not raw_sections and custom_text:
            selected_modules = []            # custom block only
        else:
            selected_modules = valid_sections
            if not selected_modules and not custom_text:
                selected_modules = None

        items, window = build_analysis_items(self.sem, focus, selected_modules)
        catalog_list = module_catalog()
        catalog = {m["id"]: m for m in catalog_list}
        if selected_modules is None:
            active_modules = [m["id"] for m in catalog_list if m["id"] in DEFAULT_MODULES]
        else:
            chosen = set(selected_modules)
            active_modules = [m["id"] for m in catalog_list if m["id"] in chosen]
        module_labels = [catalog[m]["label"] for m in active_modules if m in catalog]
        if custom_text and not custom_title:
            custom_title = "自定义：" + custom_text[:16] + (
                "…" if len(custom_text) > 16 else ""
            )
        section_title = custom_title or "自定义分析"

        events: list[tuple[str, dict]] = [("analysis.requested", {
            "focus": focus,
            "window": window,
            "modules": active_modules,
            "module_labels": module_labels,
            "custom_text": custom_text,
            "custom_title": custom_title,
            "items": [
                {"id": i.id, "section": i.section, "title": i.title,
                 "module": i.module, "kind": "preset"}
                for i in items
            ],
        })]
        preset_ctx = project_for_parser(state)
        preset_ctx["slots"] = {}
        entries: list[dict] = []
        failures: list[dict] = []
        seen: set[str] = set()

        for item in items:
            try:
                sub_events, entry = self._run_subquestion(
                    state, item.id, item.title, item.slots, preset_ctx,
                    is_preferred=(not entries),
                )
                dropped = None
            except SubquestionError as exc:
                if not item.slots.get("filters"):
                    self._analysis_failure(item.id, item.section, item.title,
                                           str(exc), events, failures)
                    continue
                # The session filter may not apply to this metric (e.g.
                # category is not a payment grain): retry on the full scope.
                retry_slots = {k: v for k, v in item.slots.items() if k != "filters"}
                try:
                    sub_events, entry = self._run_subquestion(
                        state, item.id, item.title, retry_slots, preset_ctx,
                        is_preferred=(not entries),
                    )
                    dropped = item.slots["filters"]
                except SubquestionError as exc2:
                    self._analysis_failure(item.id, item.section, item.title,
                                           str(exc2), events, failures)
                    continue
            self._finish_analysis_entry(
                item.id, item.section, item.title, sub_events, entry,
                events, entries, failures, seen,
                filter_dropped=bool(dropped),
                global_scope=(not item.apply_filters) and bool(window.get("filters")),
            )

        if custom_text:
            self._run_custom_block(
                state, custom_text, section_title,
                events, entries, failures, seen,
            )

        brief = {
            "modules": module_labels,
            "custom_title": custom_title,
            "focus_text": custom_text,
        }
        llm_ctx = self._analysis_llm_context(focus, window, entries)
        llm_ctx["brief"] = brief
        llm = self.analysis_writer.write(llm_ctx)
        report, summary = self._build_analysis_report(
            state, focus, window, entries, failures, llm, brief,
        )
        result_id = entries[0]["artifact"].result_id if entries else None
        all_insights = [x for e in entries for x in e["insights"]]
        events.append(("analysis.completed", {
            "report": report, "summary": summary, "result_id": result_id,
            "writer": "llm" if llm else "template",
            "core_conclusions": (llm or {}).get("core_conclusions", []),
            "watchouts": (llm or {}).get("watchouts", []),
            "modules": active_modules,
            "module_labels": module_labels,
            "custom_text": custom_text,
            "custom_title": custom_title,
            "sections": [
                {"id": e["id"], "section": e["section"], "title": e["title"],
                 "result_id": e["artifact"].result_id, "insights": e["insights"]}
                for e in entries
            ],
            "failures": failures,
            "reference_count": focus.get("reference_count", 0),
        }))
        # The report lives in the left result window; the chat bubble only
        # points at it and carries the executive summary.
        suffix = "，含自定义板块" if custom_text else ""
        chat_text = (
            f"经营分析已生成（{len(entries)} 个查询{suffix}，"
            f"{'模型解读' if llm else '规则解读'}），"
            "完整报告见左侧结果区的「经营分析」标签。"
        )
        if failures:
            chat_text += f"（{len(failures)} 个板块未完成）"
        chat_bits = [x for x in summary.split("；") if x][:3]
        if chat_bits:
            chat_text += "\n\n**核心结论**\n" + "\n".join(f"- {x}" for x in chat_bits)
        events.append(("answer.composed", {
            "text": chat_text, "result_id": result_id, "kind": "analysis",
            "insights": all_insights[:4],
        }))
        events.append(("result.presented", {"result_id": result_id}))
        return events

    def _analysis_failure(self, item_id: str, section: str, title: str, reason: str,
                          events: list[tuple[str, dict]], failures: list[dict]) -> None:
        failures.append({"id": item_id, "section": section,
                         "title": title, "reason": reason})
        events.append(("analysis.section.failed", {
            "id": item_id, "section": section, "title": title, "reason": reason,
        }))

    def _finish_analysis_entry(self, item_id: str, section: str, title: str,
                               sub_events: list[tuple[str, dict]], entry: dict,
                               events: list[tuple[str, dict]], entries: list[dict],
                               failures: list[dict], seen: set[str],
                               filter_dropped: bool = False,
                               global_scope: bool = False) -> None:
        fingerprint = entry["artifact"].fingerprint
        if fingerprint in seen:
            self._analysis_failure(item_id, section, title,
                                   "与已有板块的查询重复，已合并", events, failures)
            return
        seen.add(fingerprint)

        for ev_type, payload in sub_events:
            payload = dict(payload)
            payload["analysis_section"] = section
            payload["analysis_item"] = item_id
            events.append((ev_type, payload))
        artifact = entry["artifact"]
        insights = build_insights(
            entry["plan"], artifact.columns, artifact.rows, artifact.row_count,
            artifact.truncated, entry["issues"], self.sem,
        )
        entry.update({"section": section, "title": title, "insights": insights,
                      "filter_dropped": filter_dropped,
                      "global_scope": global_scope})
        events.append(("analysis.section", {
            "id": item_id, "section": section, "title": title,
            "result_id": artifact.result_id, "insights": insights,
            "issues": entry["issues"], "filter_dropped": filter_dropped,
            "global_scope": global_scope,
        }))
        entries.append(entry)

    def _run_custom_block(self, state: SessionState, text: str, section: str,
                          events: list[tuple[str, dict]], entries: list[dict],
                          failures: list[dict], seen: set[str]) -> None:
        try:
            parsed = self.parser.parse(text, project_for_parser(state))
        except Exception as exc:
            self._analysis_failure(
                "cx0", section, section,
                f"自定义分析解析失败：{type(exc).__name__}: {exc}",
                events, failures,
            )
            return

        subs = parsed.subquestions if len(parsed.subquestions) >= 2 else []
        if subs:
            for idx, sub in enumerate(subs[:3], 1):
                self._run_custom_subquestion(
                    state, sub, f"cx{idx}", section,
                    events, entries, failures, seen,
                )
            return

        if parsed.unresolved:
            reason = "没有识别出「" + "、".join(parsed.unresolved) + "」对应的指标或维度。"
            hint = self._analysis_value_hint()
            if hint:
                reason += f"当前数据可用品类：{hint}。"
            reason += "如果指的是商品关键词，请写具体商品名（如「空气炸锅」）。"
            self._analysis_failure("cx1", section, section, reason, events, failures)
            return

        if parsed.intent_type in {"unsupported", "explain", "smalltalk", "analysis"}:
            self._analysis_failure(
                "cx1", section, section,
                parsed.explanation or "这个自定义分析维度暂时无法生成查询。",
                events, failures,
            )
            return

        slots = dict(parsed.slots or {})
        if parsed.intent_type == "lookup":
            slots["intent"] = "lookup"
            dim = parsed.lookup_dim or ((slots.get("dimensions") or [None])[0])
            if not dim:
                self._analysis_failure("cx1", section, section,
                                       "没有识别出要列举的维度", events, failures)
                return
            slots["dimensions"] = [dim]
        self._run_custom_item(
            state, "cx1", parsed.rewritten_question or text, section, slots,
            events, entries, failures, seen,
        )

    def _run_custom_subquestion(self, state: SessionState, sub: dict, cid: str,
                                section: str, events: list[tuple[str, dict]],
                                entries: list[dict], failures: list[dict],
                                seen: set[str]) -> None:
        intent = sub.get("intent_type") or "query"
        question = sub.get("question") or section
        if intent in {"unsupported", "explain", "smalltalk"}:
            self._analysis_failure(
                cid, section, question,
                sub.get("explanation") or "该子问题暂时无法生成查询。",
                events, failures,
            )
            return
        slots = dict(sub.get("slots") or {})
        if intent == "lookup":
            slots["intent"] = "lookup"
            dim = sub.get("lookup_dimension") or ((slots.get("dimensions") or [None])[0])
            if not dim:
                self._analysis_failure(cid, section, question,
                                       "没有识别出要列举的维度", events, failures)
                return
            slots["dimensions"] = [dim]
        self._run_custom_item(
            state, cid, question, section, slots,
            events, entries, failures, seen,
        )

    def _run_custom_item(self, state: SessionState, cid: str, question: str,
                         section: str, slots: dict,
                         events: list[tuple[str, dict]], entries: list[dict],
                         failures: list[dict], seen: set[str]) -> None:
        try:
            sub_events, entry = self._run_subquestion(
                state, cid, question, slots, project_for_parser(state),
                is_preferred=(not entries),
            )
        except SubquestionError as exc:
            reason = str(exc)
            if "找不到" in reason or "未找到" in reason:
                hint = self._analysis_value_hint()
                if hint and "品类" not in reason:
                    reason += f"当前数据可用品类：{hint}。"
                if "商品" in reason:
                    reason += "如果指的是具体商品，请写商品名关键词（如「空气炸锅」）。"
            self._analysis_failure(cid, section, question, reason, events, failures)
            return
        self._finish_analysis_entry(
            cid, section, question, sub_events, entry,
            events, entries, failures, seen,
        )

    @staticmethod
    def _num(value, unit: str | None = None) -> str:
        if value is None:
            return "—"
        if unit == "%":
            return f"{float(value) * 100:.2f}%"
        if isinstance(value, float):
            if unit in {"元", "分"}:
                return f"{value:,.2f}"
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    @staticmethod
    def _pct(value) -> str:
        return "—" if value is None else f"{float(value) * 100:+.2f}%"

    def _entry_metric(self, entry: dict):
        return self.sem.metric(entry["plan"].metric) if entry["plan"].metric else None

    def _entry_unit(self, entry: dict):
        metric = self._entry_metric(entry)
        return metric.raw.get("unit") if metric else None

    def _entry_scope(self, entry: dict) -> str:
        plan = entry["plan"]
        parts: list[str] = []
        metric = self._entry_metric(entry)
        if metric:
            parts.append(metric.name)
        elif plan.list_dim:
            name = self.sem.dimension(plan.list_dim).raw.get("name_zh", plan.list_dim)
            parts.append(f"{name}清单")
        if plan.comparison:
            c = plan.comparison
            parts.append(
                f"{c.current.start} 至 {c.current.end} vs {c.baseline.start} 至 {c.baseline.end}"
            )
        elif plan.time:
            parts.append(f"{plan.time.start} 至 {plan.time.end}")
        if plan.filters:
            parts.append(self._plan_filters_label(plan))
        if plan.dimensions:
            names = [self.sem.dimension(d).raw.get("name_zh", d) for d in plan.dimensions]
            parts.append("按" + "、".join(names))
        if plan.having:
            symbols = {"gte": "≥", "lte": "≤", "gt": ">", "lt": "<", "eq": "="}
            labels = {"order_count": "订单数", "value": "指标值"}
            parts.append("样本量过滤：" + "、".join(
                f"{labels.get(h.metric, h.metric)}{symbols.get(h.op, h.op)}"
                f"{int(h.value) if float(h.value).is_integer() else h.value}"
                for h in plan.having
            ))
        return "；".join(parts)

    def _entry_table(self, entry: dict) -> list[str]:
        plan, artifact = entry["plan"], entry["artifact"]
        cols, rows = artifact.columns, artifact.rows
        unit = self._entry_unit(entry)
        dims = plan.dimensions

        if plan.kind == "member_list":
            out = ["| 成员 | 覆盖订单数 |", "|---|---:|"]
            for row in rows[:10]:
                count = row[1] if len(row) > 1 else None
                out.append(f"| {row[0]} | {self._num(count)} |")
            return out

        if "current" in cols and "baseline" in cols:
            ci, bi = cols.index("current"), cols.index("baseline")
            di = cols.index("delta") if "delta" in cols else None
            gi = cols.index("growth_rate") if "growth_rate" in cols else None
            if dims:
                ordered = sorted(
                    rows,
                    key=lambda r: (r[di] if di is not None and r[di] is not None
                                   else float("-inf")),
                    reverse=True,
                )[:5]
                out = ["| 维度 | 本期 | 基期 | 变化 | 变化率 |",
                       "|---|---:|---:|---:|---:|"]
                for r in ordered:
                    name = " / ".join(str(v) for v in r[: len(dims)])
                    out.append(
                        f"| {name} | {self._num(r[ci], unit)} | {self._num(r[bi], unit)} "
                        f"| {self._num(r[di], unit) if di is not None else '—'} "
                        f"| {self._pct(r[gi]) if gi is not None else '—'} |"
                    )
                return out
            r = rows[0]
            out = ["| 项目 | 数值 |", "|---|---:|",
                   f"| 本期 | {self._num(r[ci], unit)} |",
                   f"| 基期 | {self._num(r[bi], unit)} |"]
            if di is not None:
                out.append(f"| 变化 | {self._num(r[di], unit)} |")
            if gi is not None:
                out.append(f"| 变化率 | {self._pct(r[gi])} |")
            return out

        vi = cols.index("value") if "value" in cols else None
        if vi is not None and "month" in dims:
            out = ["| 月份 | 数值 |", "|---|---:|"]
            for r in rows[:12]:
                out.append(f"| {r[0]} | {self._num(r[vi], unit)} |")
            return out

        if vi is not None and dims:
            has_ratio = "numerator" in cols and "denominator" in cols
            if has_ratio:
                ni, de = cols.index("numerator"), cols.index("denominator")
                out = ["| 维度 | 数值 | 分子 | 分母 |", "|---|---:|---:|---:|"]
                for r in rows[:5]:
                    name = " / ".join(str(v) for v in r[: len(dims)])
                    out.append(
                        f"| {name} | {self._num(r[vi], unit)} | {self._num(r[ni])} "
                        f"| {self._num(r[de])} |"
                    )
                return out
            out = ["| 维度 | 数值 |", "|---|---:|"]
            for r in rows[:5]:
                name = " / ".join(str(v) for v in r[: len(dims)])
                out.append(f"| {name} | {self._num(r[vi], unit)} |")
            return out

        if vi is not None:
            r = rows[0]
            metric = self._entry_metric(entry)
            out = ["| 项目 | 数值 |", "|---|---:|",
                   f"| {metric.name if metric else '结果'} | {self._num(r[vi], unit)} |"]
            if "numerator" in cols and "denominator" in cols:
                out.append(f"| 分子 | {self._num(r[cols.index('numerator')])} |")
                out.append(f"| 分母 | {self._num(r[cols.index('denominator')])} |")
            return out
        return []

    def _entry_caveats(self, entry: dict, limit: int = 3) -> list[str]:
        seen: set[tuple] = set()
        out: list[str] = []
        ordered = sorted(entry["issues"],
                         key=lambda i: {"error": 0, "warn": 1, "info": 2}.get(i.get("severity"), 3))
        for issue in ordered:
            key = (issue.get("id"), issue.get("title"), issue.get("detail"))
            if key in seen:
                continue
            seen.add(key)
            line = issue.get("title") or issue.get("id")
            if issue.get("detail"):
                line += f"：{issue['detail']}"
            out.append(line)
            if len(out) >= limit:
                break
        return out

    def _analysis_llm_context(self, focus: dict, window: dict,
                              entries: list[dict]) -> dict:
        sections = []
        for entry in entries:
            metric = self._entry_metric(entry)
            rows = entry["artifact"].rows
            cap = 12 if "month" in entry["plan"].dimensions else 5
            sections.append({
                "id": entry["id"],
                "section": entry["section"],
                "title": entry["title"],
                "metric": entry["plan"].metric,
                "metric_name": metric.name if metric else None,
                "unit": metric.raw.get("unit") if metric else None,
                "definition": metric.raw.get("definition") if metric else None,
                "scope": self._entry_scope(entry),
                "columns": entry["artifact"].columns,
                "rows": rows[:cap],
                "row_count": entry["artifact"].row_count,
                "insights": entry.get("insights", []),
                "caveats": self._entry_caveats(entry),
                "result_id": entry["artifact"].result_id,
            })
        return {
            "window": window,
            "focus": {
                "focus_label": focus.get("focus_label", ""),
                "reference_count": focus.get("reference_count", 0),
                "last_question": focus.get("last_question"),
            },
            "sections": sections,
        }

    def _build_analysis_report(self, state: SessionState, focus: dict, window: dict,
                               entries: list[dict], failures: list[dict],
                               llm: dict | None = None,
                               brief: dict | None = None) -> tuple[str, str]:
        lo, hi = self.sem.time_range
        brief = brief or {}
        lines = ["# 经营分析报告", ""]
        if brief.get("focus_text") and not brief.get("modules"):
            # Custom-only reports have no shared trend/comparison window.
            lines.append("**分析期间**：以自定义板块的查询时间为准")
        else:
            lines.append(
                f"**分析期间**：趋势 {window['trend']['start']} 至 {window['trend']['end']}；"
                f"对比 {window['current']['start']} 至 {window['current']['end']} vs "
                f"{window['baseline']['start']} 至 {window['baseline']['end']}"
            )
        scope = "已送达订单口径"
        if focus.get("focus_label"):
            scope += f"；会话聚焦 {focus['focus_label']}"
        lines.append(f"**数据范围**：{scope}；数据覆盖 {lo} 至 {hi}")
        lines.append(
            "**分析依据**：DuckDB 只读实时查询；会话历史仅提供筛选与时间锚点；"
            "模型只解读已算出的结果，不直接访问数据库。"
        )
        if brief.get("modules"):
            lines.append(f"**本次关注**：{'、'.join(brief['modules'])}")
        if brief.get("focus_text"):
            lines.append(f"**自定义分析**：{brief['focus_text']}")
        ref = int(focus.get("reference_count") or 0)
        if ref:
            last_q = focus.get("last_question")
            lines.append(f"**会话参考**：{ref} 条已执行结果"
                         + (f"；最近问题：{last_q}" if last_q else ""))
        if failures:
            lines.append(f"**未完成板块**：{len(failures)} 个（见文末）")
        lines.append(
            "**报告生成**：" + ("模型解读 + 确定性数据" if llm
                             else "确定性模板（模型不可用或解读未通过校验）")
        )
        lines.append("")

        llm_core = [x for x in ((llm or {}).get("core_conclusions") or []) if x]
        summary_bits = llm_core[:4] or [
            f"{e['title']}：{e['insights'][0]}"
            for e in entries if e["insights"]
        ][:4]
        summary = "；".join(summary_bits)
        if summary_bits:
            lines.append("## 核心结论")
            lines.extend(f"- {x}" for x in summary_bits)
            lines.append("")

        ordered = sorted(entries + failures, key=lambda x: x["id"])
        sections: list[str] = []
        for item in ordered:
            if item["section"] not in sections:
                sections.append(item["section"])

        for idx, section in enumerate(sections, 1):
            lines.append(f"## {idx}. {section}")
            lines.append("")
            section_entries = [e for e in ordered if e["section"] == section and "artifact" in e]
            section_failures = [f for f in failures if f["section"] == section]
            for entry in section_entries:
                metric = self._entry_metric(entry)
                lines.append(f"### {entry['title']}")
                lines.append(f"- 范围与口径：{self._entry_scope(entry)}")
                if metric:
                    lines.append(
                        f"- 依据：{metric.raw.get('definition', '')}"
                        f"（结果 {entry['artifact'].result_id}）"
                    )
                if entry.get("filter_dropped"):
                    lines.append("- 说明：当前会话筛选不适用于该指标，本节使用全量口径。")
                if entry.get("global_scope"):
                    lines.append("- 说明：该板块按全量口径，未套用会话筛选。")
                commentary = (llm or {}).get("sections", {}).get(entry["id"])
                if commentary:
                    lines.append(f"> {commentary}")
                    lines.append("")
                else:
                    lines.extend(f"- {x}" for x in entry.get("insights", []))
                lines.extend(self._entry_table(entry))
                for caveat in self._entry_caveats(entry):
                    lines.append(f"- 注意：{caveat}")
                lines.append("")
            for failure in section_failures:
                lines.append(f"- 本节未能生成：{failure['title']}（{failure['reason']}）")
            lines.append("")

        watchouts = [x for x in ((llm or {}).get("watchouts") or []) if x]
        if watchouts:
            lines.append("## 需要关注")
            lines.extend(f"- {x}" for x in watchouts)
            lines.append("")

        lines.append("## 数据说明")
        lines.append("- 所有数字均来自报告引用的查询结果，可在结果标签中查看 SQL 与校验项。")
        lines.append("- 比率类指标的「变化」为百分点，「变化率」为相对变化。")
        coverage = []
        for entry in entries:
            metric = self._entry_metric(entry)
            note = metric.raw.get("coverage_note") if metric else None
            if note and note not in coverage:
                coverage.append(note)
        for note in coverage:
            lines.append(f"- 覆盖范围：{note}")
        return "\n".join(lines).strip(), summary

    def _insight_block(self, insights: list[str]) -> str:
        if not insights:
            return ""
        return "\n\n**数据要点**\n" + "\n".join(f"- {x}" for x in insights)

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
                    slots.model_dump(mode="json"), slots.unresolved, project_for_parser(state)
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
                "fingerprint": self._fingerprint(compiled),
                "columns": compiled.columns,
                "tables_used": compiled.tables_used,
                "meta": compiled.meta,
                "issues": [i.as_dict() for i in issues if i.severity != "error"],
                "interpretation_id": iid,
                "preferred": is_pref,
            }))
            try:
                artifact = execute_prepared(compiled)
            except Exception as exc:
                events.append(("interpretation.rejected", {
                    "interpretation_id": iid, "label": interp.get("label"),
                    "message": str(exc),
                }))
                continue
            artifact.fingerprint = self._fingerprint(compiled)
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
            issues, facts = check(plan, self.sem, artifact)
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
        text = self._compose(ctx, state)
        options: list[str] = []
        if filter_failure:
            slots, reason = filter_failure
            repair = build_repair(self.sem, self._repair_slots(state, slots))
            options = repair["options"]
            text = f"{text}\n\n补充说明：{reason}\n\n{repair_text(repair)}"
        preferred_entry = next((e for e in executed if e["preferred"]), executed[0])
        preferred_art = preferred_entry["artifact"]
        insights = build_insights(
            preferred_entry["plan"], preferred_art.columns, preferred_art.rows,
            preferred_art.row_count, preferred_art.truncated,
            preferred_entry["issues"], self.sem,
        )
        text += self._insight_block(insights)
        events.append(("answer.composed", {
            "text": text,
            "result_id": next(
                (e["artifact"].result_id for e in executed if e["preferred"]),
                executed[0]["artifact"].result_id,
            ),
            "insights": insights,
            "interpretations": [
                {"id": e["id"], "label": e["label"], "preferred": e["preferred"]}
                for e in executed
            ],
            "options": options,
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
        ctx = project_for_parser(state)
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
            message = f"{exc}\n\n{UNSUPPORTED_SUGGESTIONS}"
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
            "fingerprint": self._fingerprint(compiled),
            "columns": compiled.columns,
            "tables_used": compiled.tables_used,
            "meta": compiled.meta,
            "issues": [i.as_dict() for i in issues if i.severity != "error"],
        }

    def _handle_executing(self, state: SessionState) -> list[tuple[str, dict]]:
        plan = QueryPlan.model_validate(state.plan.model_dump())
        try:
            compiled, _ = prepare(plan, self.sem)
            result = execute_prepared(compiled)
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
        result.fingerprint = self._fingerprint(compiled)
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
            result = execute_prepared(compiled)
            result.fingerprint = self._fingerprint(compiled)
            result.result_id = f"r{state.turn}-{result.fingerprint}"
            self._results[(state.session_id, state.turn)] = result
        issues, facts = check(plan, self.sem, result)
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
        artifact = state.artifacts.get(state.last_result_id or "") or {}
        if state.plan is not None and state.plan.kind == "member_list":
            rows = artifact.get("rows") or []
            text = lookup_mod.describe(state.plan.list_dim or "", rows, len(rows))
            scope: list[str] = []
            if state.plan.filters:
                scope.append(self._plan_filters_label(state.plan))
            if state.plan.time:
                scope.append(f"{state.plan.time.start} 至 {state.plan.time.end}")
            if scope:
                text = f"限定范围：{'；'.join(scope)}。\n\n{text}"
            insights = self._payload_insights(state.plan, artifact, state.checks)
            text += self._insight_block(insights)
            return [
                ("answer.composed", {"text": text, "result_id": state.last_result_id,
                                     "insights": insights}),
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
        insights = self._payload_insights(state.plan, artifact, state.checks)
        text += self._insight_block(insights)
        events.append(("answer.composed", {"text": text, "result_id": state.last_result_id,
                                           "insights": insights}))
        events.append(("result.presented", {"result_id": state.last_result_id}))
        return events

    def _payload_insights(self, plan, artifact: dict, issues: list[dict]) -> list[str]:
        if not plan or not artifact:
            return []
        if any(i.get("severity") == "error" for i in (issues or [])):
            return []
        return build_insights(
            plan, artifact.get("columns") or [], artifact.get("rows") or [],
            int(artifact.get("row_count") or 0), bool(artifact.get("truncated")),
            issues or [], self.sem,
        )

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
        shown = humanize_slots(raw).get("filters", {})
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
