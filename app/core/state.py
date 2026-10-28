"""Session state + pure reducer.

`reduce(events) -> SessionState` has no IO and no model calls. Given the same
event log it always produces the same state, which is what makes replay and
audit possible.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field

from app.config.settings import DEFAULT_DATASET_ID
from app.domain.plan import (
    CompareSpec,
    ComparisonSpec,
    FilterSpec,
    HavingSpec,
    NestedSpec,
    OrderSpec,
    QueryPlan,
    TimeRange,
)


class Status(str, Enum):
    IDLE = "IDLE"
    PARSING = "PARSING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    GROUNDING = "GROUNDING"
    PLANNING = "PLANNING"
    VALIDATING = "VALIDATING"
    EXECUTING = "EXECUTING"
    CHECKING = "CHECKING"
    COMPOSING = "COMPOSING"
    DONE = "DONE"
    FAILED = "FAILED"


class Slots(BaseModel):
    metric: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    time: TimeRange | None = None
    filters: dict[str, FilterSpec] = Field(default_factory=dict)
    having: list[HavingSpec] = Field(default_factory=list)
    compare: CompareSpec | None = None
    comparison: ComparisonSpec | None = None
    order_by: list[OrderSpec] = Field(default_factory=list)
    limit: int = 100
    nested: NestedSpec | None = None
    intent: str = "query"          # query | lookup | coverage | explain
    coverage_period: str = "month"
    unresolved: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    visualization: dict | None = None


@dataclass
class FailurePolicy:
    max_retry: int = 1
    retries: dict[str, int] = field(default_factory=dict)

    def can_retry(self, stage: str) -> bool:
        return self.retries.get(stage, 0) < self.max_retry


@dataclass
class SessionState:
    session_id: str
    dataset_id: str = DEFAULT_DATASET_ID
    status: Status = Status.IDLE
    slots: Slots = field(default_factory=Slots)
    prev_slots: Slots = field(default_factory=Slots)
    plan: QueryPlan | None = None
    artifacts: dict[str, dict] = field(default_factory=dict)
    pending: dict | None = None
    turn: int = 0
    last_seq: int = 0
    error: dict | None = None
    policy: FailurePolicy = field(default_factory=FailurePolicy)
    messages: list[dict] = field(default_factory=list)
    intent: dict = field(default_factory=dict)
    summary: str | None = None
    clarifications: list[dict] = field(default_factory=list)
    subquestions: list[str] = field(default_factory=list)
    analysis: dict | None = None
    last_result_id: str | None = None
    preferred_result_id: str | None = None
    checks: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    bindings: list[dict] = field(default_factory=list)
    interpretations: list[dict] = field(default_factory=list)
    preferred_interpretation: str | None = None
    interpretation_results: list[dict] = field(default_factory=list)
    context: dict | None = None
    trace_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "dataset_id": self.dataset_id,
            "status": self.status.value,
            "turn": self.turn,
            "slots": self.slots.model_dump(mode="json"),
            "plan": self.plan.model_dump(mode="json") if self.plan else None,
            "pending": self.pending,
            "error": self.error,
            "last_result_id": self.last_result_id,
            "preferred_result_id": self.preferred_result_id,
            "checks": self.checks,
            "facts": self.facts,
            "bindings": self.bindings,
            "interpretations": self.interpretations,
            "preferred_interpretation": self.preferred_interpretation,
            "interpretation_results": self.interpretation_results,
            "context": self.context,
            "analysis": self.analysis,
            "summary": self.summary,
            "last_seq": self.last_seq,
        }


def slots_has_content(slots: Slots) -> bool:
    return bool(slots.metric or slots.dimensions or slots.filters or slots.time)


def _slots_from_payload(payload: dict, base: Slots) -> Slots:
    data = payload.get("slots", payload)
    merged = base.model_dump()
    for key, value in data.items():
        if key in merged and value is not None:
            merged[key] = value
    return Slots.model_validate(merged)


def _apply_grounding(slots: Slots, payload: dict) -> Slots:
    new = slots.model_copy(deep=True)
    bindings = payload.get("bindings")
    if bindings:
        for b in bindings:
            dim, ids = b.get("dim"), b.get("ids") or b.get("values") or []
            if dim and ids:
                new.filters[dim] = FilterSpec(op="in", value=list(ids))
    else:
        dim = payload.get("dim")
        ids = payload.get("ids") or payload.get("values") or []
        if dim and ids:
            new.filters[dim] = FilterSpec(op="in", value=list(ids))
    return new


def reduce(events: list, session_id: str | None = None) -> SessionState:
    sid = session_id or (events[0].session_id if events else "")
    state = SessionState(session_id=sid)
    for ev in sorted(events, key=lambda e: e.seq):
        state.last_seq = ev.seq
        if ev.trace_id:
            state.trace_id = ev.trace_id
        t, p = ev.type, ev.payload

        if t == "session.created":
            dataset_id = p.get("dataset_id")
            if isinstance(dataset_id, str) and dataset_id:
                state.dataset_id = dataset_id
        elif t == "turn.started":
            state.turn = int(p.get("turn", state.turn + 1))
            state.plan = None
            state.error = None
            state.checks = []
            state.facts = {}
            state.analysis = None
            state.context = None
        elif t == "user.message":
            text = p.get("text", "")
            state.turn = max(state.turn, int(p.get("turn", state.turn + 1)))
            state.error = None
            state.checks = []
            state.facts = {}
            state.interpretations = []
            state.preferred_interpretation = None
            state.interpretation_results = []
            state.preferred_result_id = None
            state.messages.append({"role": "user", "text": text, "seq": ev.seq})
            if state.status is Status.AWAITING_CLARIFICATION and state.pending:
                # This message is an answer to the pending clarification, not a
                # new question: keep the slots and bind the answer to the field.
                pending = state.pending
                field = pending.get("field")
                if pending.get("partial_slots"):
                    # restore what we already understood before the ambiguity
                    state.slots = Slots.model_validate(pending["partial_slots"])
                state.clarifications.append({
                    "field": field,
                    "value": text,
                    "raw": text,
                    "candidates": pending.get("candidates", []),
                })
                if field and field not in {"unknown", "metric"}:
                    state.slots.filters[field] = FilterSpec(op="eq", value=text)
                    state.status = Status.GROUNDING
                else:
                    state.status = Status.PARSING
                state.pending = None
            else:
                # Keep the previous turn's slots for follow-up inheritance,
                # then start this turn from a clean slate.
                state.prev_slots = state.slots.model_copy(deep=True)
                state.status = Status.PARSING
                state.plan = None
                state.slots = Slots()
        elif t == "intent.classified":
            state.intent = p
        elif t == "query.normalized":
            if p.get("notes"):
                state.slots.notes = list(dict.fromkeys(state.slots.notes + p["notes"]))
        elif t == "context.compiled":
            state.context = p
        elif t == "intent.parsed":
            state.slots = _slots_from_payload(p, state.slots)
            state.interpretations = list(p.get("interpretations") or [])
            state.preferred_interpretation = p.get("preferred_interpretation")
            state.status = Status.GROUNDING
        elif t == "intent.invalid":
            state.status = Status.PARSING
            state.error = {"code": "INTENT_INVALID", "message": "无法解析问题", "detail": p}
        elif t == "subquestions.planned":
            state.subquestions = list(p.get("questions", []))
        elif t == "analysis.requested":
            state.analysis = {"status": "running", **p}
        elif t == "analysis.completed":
            state.analysis = {"status": "done", **p}
            state.status = Status.DONE
        elif t == "subquestions.completed":
            if p.get("primary_slots"):
                state.slots = Slots.model_validate(p["primary_slots"])
            state.status = Status.DONE
        elif t == "ambiguity.detected":
            state.status = Status.AWAITING_CLARIFICATION
            state.pending = p
        elif t == "clarification.asked":
            state.status = Status.AWAITING_CLARIFICATION
            state.pending = p
        elif t == "clarification.answered":
            state.clarifications.append(p)
            if p.get("dim") and p.get("ids"):
                state.slots = _apply_grounding(state.slots, p)
            elif p.get("slots_patch"):
                state.slots = _slots_from_payload(p["slots_patch"], state.slots)
            state.pending = None
            state.status = Status.GROUNDING
        elif t == "values.grounded":
            state.bindings = list(p.get("bindings", []))
            state.slots = _apply_grounding(state.slots, p)
            if p.get("notes"):
                state.slots.notes = list(dict.fromkeys(state.slots.notes + p["notes"]))
            state.status = Status.PLANNING
        elif t == "plan.compiled":
            state.plan = QueryPlan.model_validate(p.get("plan", p))
            state.status = Status.VALIDATING
        elif t == "plan.rejected":
            state.status = Status.PLANNING if p.get("recoverable", False) else Status.FAILED
            state.error = {"code": "PLAN_REJECTED", "message": p.get("reason", "计划被拒绝"),
                           "issues": p.get("issues", [])}
        elif t == "sql.compiled":
            state.status = Status.EXECUTING
        elif t == "sql.executed":
            rid = p.get("result_id", "")
            state.artifacts[rid] = p
            if p.get("interpretation_id"):
                entry = {
                    "id": p.get("interpretation_id"),
                    "label": p.get("interpretation_label"),
                    "preferred": bool(p.get("preferred")),
                    "result_id": rid,
                    "question": p.get("question"),
                    "row_count": p.get("row_count"),
                    "display": p.get("display"),
                    "meta": p.get("meta"),
                }
                state.interpretation_results = [
                    r for r in state.interpretation_results
                    if r.get("id") != entry["id"]
                ] + [entry]
            if p.get("preferred"):
                state.preferred_result_id = rid
                state.last_result_id = rid
            elif not p.get("interpretation_id"):
                # single-plan query: always point at the fresh result
                state.last_result_id = rid
            elif not state.preferred_result_id:
                state.last_result_id = rid
            state.status = Status.CHECKING
        elif t == "checks.completed":
            result_id = p.get("result_id")
            if result_id in state.artifacts:
                state.artifacts[result_id]["checks"] = p.get("issues", [])
                state.artifacts[result_id]["facts"] = p.get("facts", {})
                state.artifacts[result_id]["blocked"] = bool(p.get("blocked"))
            if p.get("interpretation_id"):
                for entry in state.interpretation_results:
                    if entry.get("id") == p["interpretation_id"]:
                        entry["checks"] = p.get("issues", [])
                        entry["facts"] = p.get("facts", {})
                        entry["blocked"] = bool(p.get("blocked"))
                if p.get("preferred") or p["interpretation_id"] == state.preferred_interpretation:
                    state.checks = p.get("issues", [])
                    state.facts = p.get("facts", {})
            else:
                state.checks = p.get("issues", [])
                state.facts = p.get("facts", {})
            state.status = Status.COMPOSING
        elif t == "answer.composed":
            state.messages.append({"role": "assistant", "text": p.get("text", ""), "seq": ev.seq})
            state.status = Status.DONE
        elif t == "result.presented":
            state.last_result_id = p.get("result_id", state.last_result_id)
            state.status = Status.DONE
        elif t == "context.compacted":
            pass
        elif t == "session.summary":
            state.summary = p.get("summary")
        elif t == "llm.retry":
            stage = p.get("stage", "unknown")
            state.policy.retries[stage] = state.policy.retries.get(stage, 0) + 1
        elif t == "error.raised":
            state.error = p
            if not p.get("recoverable", False):
                state.status = Status.FAILED
        elif t == "turn.completed":
            if state.status not in {Status.DONE, Status.FAILED, Status.AWAITING_CLARIFICATION}:
                state.status = Status.DONE
        # unknown event types are ignored on purpose (forward compatibility)
    return state


def clone(state: SessionState) -> SessionState:
    return deepcopy(state)
