"""Context projections: three audiences, three shapes.

- parser:    current question + recent turns + slot digest (no result rows)
- composer:  result preview + checks + ranges (no raw event history)
- ui:        event-sourced view for the canvas

Projections are pure functions of SessionState. Slots are never compressed.
"""
from __future__ import annotations

import json

import json

from app.config.settings import DIM_VALUES_PATH, KEEP_RECENT_TURNS
from app.core.state import SessionState, slots_has_content

_ID_REVERSE_CACHE: dict | None = None


def _id_reverse() -> dict[str, dict]:
    """dim -> {id: display name}, built once from the generated catalog."""
    global _ID_REVERSE_CACHE
    if _ID_REVERSE_CACHE is not None:
        return _ID_REVERSE_CACHE
    out: dict[str, dict] = {}
    try:
        data = json.loads(DIM_VALUES_PATH.read_text(encoding="utf-8"))
        for dim, mapping in (data.get("id_maps") or {}).items():
            out[dim] = {str(v): str(k) for k, v in mapping.items()}
    except Exception:
        out = {}
    _ID_REVERSE_CACHE = out
    return out


def humanize_slots(slots: dict) -> dict:
    """Replace internal IDs with display names before showing slots to the LLM."""
    out = json.loads(json.dumps(slots))
    rev = _id_reverse()
    for dim, spec in (out.get("filters") or {}).items():
        if dim not in rev or not isinstance(spec, dict):
            continue
        value = spec.get("value")
        values = value if isinstance(value, list) else [value]
        spec["value"] = [rev[dim].get(str(v), v) for v in values]
        if not isinstance(value, list) and len(spec["value"]) == 1:
            spec["value"] = spec["value"][0]
    return out


def estimate_tokens(value) -> int:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    # conservative for mixed Chinese/ASCII
    return max(1, len(value) // 2)


def project_for_parser(state: SessionState, keep_turns: int = KEEP_RECENT_TURNS) -> dict:
    recent = state.messages[-keep_turns * 2:]
    # While PARSING, `slots` still holds this turn's (empty) container; the
    # parser needs the previous turn's slots to resolve "那5月呢"-style follow-ups.
    base = state.slots if slots_has_content(state.slots) else state.prev_slots
    return {
        "session_id": state.session_id,
        "turn": state.turn,
        "status": state.status.value,
        "recent_messages": [
            {"role": m["role"], "text": m["text"]} for m in recent
        ],
        "slots": humanize_slots(base.model_dump(mode="json")),
        "current_slots": state.slots.model_dump(mode="json"),
        "pending_clarification": state.pending,
        "summary": state.summary,
    }


def project_for_composer(state: SessionState, max_rows: int = 50) -> dict:
    artifact = state.artifacts.get(state.last_result_id or "", {})
    rows = artifact.get("rows") or []
    return {
        "session_id": state.session_id,
        "question": artifact.get("question") or _last_user_text(state),
        "metric": (state.plan.metric if state.plan else None),
        "dimensions": state.plan.dimensions if state.plan else [],
        "time": state.plan.time.model_dump(mode="json") if state.plan and state.plan.time else None,
        "filters": {k: v.model_dump(mode="json") for k, v in state.slots.filters.items()},
        "columns": artifact.get("columns", []),
        "rows": rows[:max_rows],
        "row_count": artifact.get("row_count", 0),
        "truncated": artifact.get("truncated", False),
        "sql": artifact.get("sql"),
        "checks": state.checks,
        "facts": state.facts,
        "notes": state.slots.notes,
    }


def project_for_ui(state: SessionState) -> dict:
    return state.to_dict()


def _last_user_text(state: SessionState) -> str:
    for m in reversed(state.messages):
        if m["role"] == "user":
            return m["text"]
    return ""
