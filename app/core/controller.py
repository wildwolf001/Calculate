"""The driving loop: state machine owns transitions, model is a subroutine.

Every step appends exactly one event. The loop re-reads the event log each
iteration (`reduce`), so an in-memory object can never drift from history.
"""
from __future__ import annotations

import traceback
from typing import AsyncIterator

from app.config.settings import MAX_STEPS
from app.core.context import project_for_composer, project_for_parser
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
                 composer: AnswerComposer, tracer: Tracer | None = None):
        self.store = store
        self.sem = sem
        self.parser = parser
        self.grounder = grounder
        self.composer = composer
        self.tracer = tracer
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
            events.append(("subquestions.planned", {
                "questions": [q.get("question", "") for q in result.subquestions],
                "depth": 1,
            }))
            if result.subquestions[0].get("slots"):
                slots = result.subquestions[0]["slots"]
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
                "fingerprint": compiled.fingerprint(self.sem.version),
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
            artifact.result_id = f"r{state.turn}-{iid}-{compiled.fingerprint(self.sem.version)}"
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
        result.result_id = f"r{state.turn}-{compiled.fingerprint(self.sem.version)}"
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
            result.result_id = f"r{state.turn}-{compiled.fingerprint(self.sem.version)}"
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
        if state.plan is not None and state.plan.kind == "member_list":
            artifact = state.artifacts.get(state.last_result_id or "") or {}
            rows = artifact.get("rows") or []
            text = lookup_mod.describe(state.plan.list_dim or "", rows, len(rows))
            scope: list[str] = []
            if state.plan.filters:
                scope.append(self._plan_filters_label(state.plan))
            if state.plan.time:
                scope.append(f"{state.plan.time.start}~{state.plan.time.end}")
            if scope:
                text = f"限定范围：{'；'.join(scope)}。\n\n{text}"
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
        events.append(("answer.composed", {"text": text, "result_id": state.last_result_id}))
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
