"""Event definitions. Events are append-only facts; they never decide anything."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


EVENT_TYPES = {
    "user.message",
    "intent.classified",
    "query.normalized",
    "context.compiled",
    "intent.parsed",
    "query.ir.created",
    "intent.repaired",
    "intent.invalid",
    "subquestions.planned",
    "subquestion.started",
    "subquestion.answered",
    "subquestion.rejected",
    "subquestions.completed",
    "analysis.requested",
    "analysis.section",
    "analysis.section.failed",
    "analysis.filter_dropped",
    "analysis.completed",
    "ambiguity.detected",
    "clarification.asked",
    "clarification.answered",
    "values.grounded",
    "capability.assessed",
    "refusal.decided",
    "execution.failed",
    "repair.applied",
    "diagnosis.completed",
    "tool.called",
    "tool.result",
    "plan.compiled",
    "plan.repaired",
    "plan.repair_requested",
    "plan.rejected",
    "sql.compiled",
    "sql.executed",
    "checks.completed",
    "answer.composed",
    "result.presented",
    "context.compacted",
    "session.summary",
    "llm.retry",
    "error.raised",
    "turn.started",
    "turn.completed",
    "session.created",
}


@dataclass(frozen=True)
class Event:
    seq: int
    session_id: str
    ts: datetime
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    caused_by: str | None = None
    trace_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "session_id": self.session_id,
            "ts": self.ts.isoformat(timespec="milliseconds"),
            "type": self.type,
            "payload": self.payload,
            "caused_by": self.caused_by,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            seq=int(data["seq"]),
            session_id=data["session_id"],
            ts=datetime.fromisoformat(data["ts"]),
            type=data["type"],
            payload=data.get("payload") or {},
            caused_by=data.get("caused_by"),
            trace_id=data.get("trace_id"),
        )
