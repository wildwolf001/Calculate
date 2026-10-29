"""Context projections: three audiences, three shapes.

- parser:    current question + recent turns + slot digest (no result rows)
- composer:  result preview + checks + ranges (no raw event history)
- ui:        event-sourced view for the canvas

Projections are pure functions of SessionState. Slots are never compressed.
"""
from __future__ import annotations

import json

from app.config.settings import DIM_VALUES_PATH, KEEP_RECENT_TURNS
from app.core.state import SessionState, slots_has_content
from app.domain.presentation import summarize_rows
from app.domain.semantic.loader import SemanticLayer

_ID_REVERSE_CACHE: dict[str, dict[str, dict]] = {}


def _id_reverse(dim_values_path=DIM_VALUES_PATH) -> dict[str, dict]:
    """dim -> {id: display name}, cached per dataset artifact file."""
    key = str(dim_values_path)
    if key in _ID_REVERSE_CACHE:
        return _ID_REVERSE_CACHE[key]
    out: dict[str, dict] = {}
    try:
        data = json.loads(dim_values_path.read_text(encoding="utf-8"))
        for dim, mapping in (data.get("id_maps") or {}).items():
            out[dim] = {str(v): str(k) for k, v in mapping.items()}
    except Exception:
        out = {}
    _ID_REVERSE_CACHE[key] = out
    return out


def humanize_slots(slots: dict, dim_values_path=DIM_VALUES_PATH) -> dict:
    """Replace internal IDs with display names before showing slots to the LLM."""
    out = json.loads(json.dumps(slots))
    rev = _id_reverse(dim_values_path)
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


def _scope_from_plan(plan: dict | None) -> dict:
    plan = plan or {}
    return {
        "metric": plan.get("metric"),
        "dimensions": list(plan.get("dimensions") or []),
        "time": plan.get("time"),
        "comparison": plan.get("comparison"),
        "filters": plan.get("filters") or {},
    }


def _focus_dimension(sem: SemanticLayer, metric_id: str | None) -> str | None:
    if not metric_id or metric_id not in sem.metrics:
        return None
    metric = sem.metrics[metric_id]
    explicit = metric.raw.get("focus_dimension") or metric.raw.get("entity_dimension")
    if explicit in sem.dimensions:
        return explicit
    name = metric.name
    hints = (
        ("商家", ("商家名称", "商家", "卖家")),
        ("卖家", ("商家名称", "商家", "卖家")),
        ("商品", ("商品名称", "商品", "产品")),
        ("产品", ("商品名称", "商品", "产品")),
        ("客户", ("客户名称", "客户", "顾客")),
        ("品类", ("品类", "类目", "分类")),
    )
    for metric_word, dim_words in hints:
        if metric_word not in name:
            continue
        for dim_id, dim in sem.dimensions.items():
            dim_name = str(dim.raw.get("name_zh", dim_id))
            if dim_name in dim_words:
                return dim_id
    return None


def build_conversation_context(state: SessionState, sem: SemanticLayer,
                               keep_turns: int = KEEP_RECENT_TURNS) -> dict:
    """Deterministically compile event-sourced state for semantic parsing.

    The event log remains the source of truth. This projection exposes typed
    scope, result and focus facts; it never asks a model to reconstruct them.
    """
    artifact = state.artifacts.get(state.last_result_id or "") or {}
    plan = artifact.get("plan") or (
        state.plan.model_dump(mode="json") if state.plan else None
    )
    active_scope = _scope_from_plan(plan)
    previous_scope = state.prev_slots.model_dump(mode="json")

    rows = artifact.get("rows") or []
    columns = artifact.get("columns") or []
    focus: dict = {}
    if plan:
        metric_id = plan.get("metric")
        metric = sem.metrics.get(metric_id)
        if plan.get("kind") == "member_list":
            entity_dim = plan.get("list_dim")
            values = [row[0] for row in rows if row]
            focus = {
                "kind": "member_set",
                "entity_dimension": entity_dim,
                "values": values[:20],
                "count": artifact.get("row_count"),
            }
        elif plan.get("dimensions"):
            entity_dim = plan["dimensions"][0]
            values = [row[0] for row in rows if row]
            focus = {
                "kind": "grouped_result",
                "entity_dimension": entity_dim,
                "values": values[:20],
                "count": artifact.get("row_count"),
            }
        elif metric:
            entity_dim = _focus_dimension(sem, metric_id)
            count = None
            if rows and columns and "value" in columns:
                value = rows[0][columns.index("value")]
                if isinstance(value, int):
                    count = value
            focus = {
                "kind": "entity_count" if entity_dim else "aggregate",
                "entity_dimension": entity_dim,
                "expected_count": count,
                "metric_name": metric.name,
            }

    recent_messages = [
        {
            "role": message.get("role"),
            "text": message.get("text"),
            "seq": message.get("seq"),
        }
        for message in state.messages[-keep_turns * 2:]
    ]
    return {
        "session_id": state.session_id,
        "dataset_id": state.dataset_id,
        "turn": state.turn,
        "active_result_id": state.last_result_id,
        "active_scope": active_scope,
        "previous_scope": previous_scope,
        "focus": focus,
        "recent_messages": recent_messages,
        "pending_clarification": state.pending,
        "summary": state.summary,
    }


def apply_context_action(result, context: dict | None) -> None:
    """Apply a parser's explicit context decision without lexical guessing."""
    if context is None or result.intent_type not in {"query", "lookup"}:
        return
    action = result.context_action or "new"
    if action == "new":
        return
    if action == "reference_result":
        focus = context.get("focus") or {}
        if (
            result.intent_type == "query"
            and not result.slots.get("metric")
            and focus.get("entity_dimension")
        ):
            result.intent_type = "lookup"
            result.lookup_dim = focus["entity_dimension"]
            result.slots["intent"] = "lookup"
            result.slots["dimensions"] = [focus["entity_dimension"]]
        base = context.get("active_scope") or {}
    else:
        base = context.get("active_scope") or context.get("previous_scope") or {}

    slots = result.slots
    if result.intent_type == "lookup":
        # Member-list plans have their own grain and time coverage. They inherit
        # the business scope (filters), never the aggregate metric/comparison
        # or an aggregate time window that may not be supported by the
        # dimension table.
        inherited_filters = dict(base.get("filters") or {})
        inherited_filters.update(slots.get("filters") or {})
        slots["filters"] = inherited_filters
        slots["metric"] = None
        if action == "reference_result":
            # A count result can be expanded to the member set under the same
            # business filters. The aggregate period is not necessarily a
            # supported time grain for member-list compilation.
            slots["time"] = None
            slots["comparison"] = None
        return

    for key in ("metric", "time", "comparison"):
        if slots.get(key) is None and base.get(key) is not None:
            slots[key] = base[key]
    if action in {"inherit", "refine"} and not slots.get("dimensions"):
        slots["dimensions"] = list(base.get("dimensions") or [])
    if action == "reference_result" and not slots.get("dimensions"):
        slots["dimensions"] = list(base.get("dimensions") or [])
    inherited_filters = dict(base.get("filters") or {})
    inherited_filters.update(slots.get("filters") or {})
    slots["filters"] = inherited_filters


def _humanize_context(context: dict, dim_values_path) -> dict:
    out = json.loads(json.dumps(context, ensure_ascii=False, default=str))
    for scope_name in ("active_scope", "previous_scope"):
        scope = out.get(scope_name) or {}
        scope["filters"] = humanize_slots(
            {"filters": scope.get("filters") or {}}, dim_values_path
        ).get("filters", {})
    return out


def context_for_event(context: dict) -> dict:
    """Trim parser-only prose from the durable/public context event."""
    safe = {
        key: value for key, value in context.items()
        if key not in {"recent_messages", "pending_clarification"}
    }
    focus = safe.get("focus")
    if isinstance(focus, dict):
        focus = dict(focus)
        values = focus.get("values")
        if isinstance(values, list):
            focus["values"] = values[:8]
        safe["focus"] = focus
    return safe


def project_for_parser(state: SessionState, keep_turns: int = KEEP_RECENT_TURNS,
                       *, dim_values_path=DIM_VALUES_PATH,
                       conversation_context: dict | None = None) -> dict:
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
        "slots": humanize_slots(base.model_dump(mode="json"), dim_values_path),
        "current_slots": state.slots.model_dump(mode="json"),
        "conversation_context": _humanize_context(
            conversation_context or {}, dim_values_path
        ),
        "pending_clarification": state.pending,
        "summary": state.summary,
    }


def project_for_composer(state: SessionState, max_rows: int = 50) -> dict:
    artifact = state.artifacts.get(state.last_result_id or "", {})
    rows = artifact.get("rows") or []
    columns = artifact.get("columns", [])
    dimensions = state.plan.dimensions if state.plan else []
    ctx = {
        "session_id": state.session_id,
        "question": artifact.get("question") or _last_user_text(state),
        "metric": (state.plan.metric if state.plan else None),
        "dimensions": dimensions,
        "time": state.plan.time.model_dump(mode="json") if state.plan and state.plan.time else None,
        "filters": {k: v.model_dump(mode="json") for k, v in state.slots.filters.items()},
        "columns": columns,
        "row_count": artifact.get("row_count", 0),
        "truncated": artifact.get("truncated", False),
        "sql": artifact.get("sql"),
        "checks": state.checks,
        "facts": state.facts,
        "notes": state.slots.notes,
    }
    ctx.update(summarize_rows(rows, columns, dimensions, max_rows))
    return ctx


def project_for_ui(state: SessionState) -> dict:
    return state.to_dict()


def _last_user_text(state: SessionState) -> str:
    for m in reversed(state.messages):
        if m["role"] == "user":
            return m["text"]
    return ""
