"""Context compaction with three graded levels.

L1  projection-level trimming of result rows (no event);
L2  summarize older turns into `session.summary` (event, auditable);
L3  drop artifact bodies older than the latest few (event, auditable).

Compaction never touches slots, the plan, or the current result.
"""
from __future__ import annotations

from app.config.settings import CONTEXT_TOKEN_BUDGET, KEEP_RECENT_TURNS
from app.core.context import estimate_tokens
from app.core.state import SessionState

ARTIFACT_KEEP = 3


def _transcript_tokens(state: SessionState) -> int:
    return sum(estimate_tokens(m.get("text", "")) for m in state.messages)


def summarize_messages(state: SessionState, keep_turns: int = KEEP_RECENT_TURNS) -> str:
    old = state.messages[: -keep_turns * 2] if len(state.messages) > keep_turns * 2 else []
    lines = []
    for m in old:
        role = "用户" if m["role"] == "user" else "助手"
        text = (m.get("text") or "").replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"{role}: {text}")
    if not lines:
        return state.summary or ""
    prefix = (state.summary + "\n") if state.summary else ""
    return prefix + "\n".join(lines)


def plan_compaction(state: SessionState) -> list[tuple[str, dict]]:
    """Return the events needed to bring context back under budget."""
    events: list[tuple[str, dict]] = []
    tokens = _transcript_tokens(state)
    if tokens > CONTEXT_TOKEN_BUDGET and len(state.messages) > KEEP_RECENT_TURNS * 2:
        summary = summarize_messages(state)
        events.append(("session.summary", {
            "summary": summary,
            "covered_seq_range": [state.messages[0].get("seq", 0), state.messages[-KEEP_RECENT_TURNS * 2].get("seq", state.last_seq)],
            "tokens_before": tokens,
            "level": "L2",
        }))
        events.append(("context.compacted", {
            "level": "L2",
            "freed_tokens": max(0, tokens - estimate_tokens(summary)),
            "kept_turns": KEEP_RECENT_TURNS,
        }))
    if len(state.artifacts) > ARTIFACT_KEEP:
        stale = sorted(state.artifacts)[:-ARTIFACT_KEEP]
        events.append(("context.compacted", {
            "level": "L3",
            "dropped_result_ids": stale,
            "kept": ARTIFACT_KEEP,
        }))
    return events
