# Entity-count shape detection and metric binding.
#
# The parser must not guess between an order count and 'customers with at
# least N orders' from lexical overlap.  This module recognises the generic
# Chinese count pattern, carries entity + threshold as a typed shape, and
# binds it only to published entity_count metrics.  If no such metric exists
# it returns an unresolved entity instead of falling back to an order count.
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.domain.plan import EntityCountSpec
from app.domain.semantic.loader import SemanticLayer


_THRESHOLD = re.compile(r"(?:至少|不少于)\s*(\d+)\s*(?:笔|单|次|个|张|份|条)")
_COUNT_PREFIX = re.compile(r"(?:有多少|多少个|多少名|多少位|几个|几位)\s*")
_ENTITY_BEFORE_THRESHOLD = re.compile(
    r"(?P<entity>[\u4e00-\u9fffA-Za-z_]{2,8}?)"
    r"(?=完成|下过|买过|有|至少|不少于)"
)
_ENTITY_BEFORE_COUNT = re.compile(
    r"(?P<entity>[\u4e00-\u9fff]{2,6})有多少"
)
_ENTITY_VERB = re.compile(
    r"(?:有多少|多少个|多少名|多少位|几个|几位)\s*"
    r"(?P<entity>[\u4e00-\u9fffA-Za-z_]{2,8}?)"
    r"(?=下过|买过|有|完成过|用过|来过)"
)
_ENTITY_END = re.compile(
    r"(?:有多少|多少个|多少名|多少位|几个|几位)\s*"
    r"(?P<entity>[\u4e00-\u9fffA-Za-z_]{2,8})\s*[？?]?\s*$"
)


def _clean_entity(value: str) -> str:
    value = str(value or "").strip()
    if value in {"一共", "总共", "合计", "共", "多少", "几个"}:
        return ""
    value = re.sub(r"^(?:\d+)?(?:笔|单|次|个|张|份|条)的?", "", value)
    value = re.sub(r"^(?:种|款|类)", "", value)
    return re.sub(r"^(?:的|了|过)+", "", value)


def _entity_from_text(text: str) -> str:
    match = _COUNT_PREFIX.search(text)
    if match:
        rest = text[match.end():]
        found = _ENTITY_BEFORE_THRESHOLD.search(rest)
        if found:
            return _clean_entity(found.group("entity"))
        found = _ENTITY_VERB.search(text)
        if found:
            return _clean_entity(found.group("entity"))
    found = _ENTITY_BEFORE_COUNT.search(text)
    if found:
        return _clean_entity(found.group("entity"))
    found = _ENTITY_END.search(text)
    if found:
        return _clean_entity(found.group("entity"))
    return ""


def detect_entity_count(text: str) -> EntityCountSpec | None:
    value = str(text or "")
    threshold = _THRESHOLD.search(value)
    entity = _entity_from_text(value)
    if threshold:
        if not entity:
            return None
        return EntityCountSpec(
            entity=entity, min_events=int(threshold.group(1))
        )
    if entity and re.search(r"(?:下过|买过|有|完成过|用过|来过)", value):
        return EntityCountSpec(entity=entity, min_events=1)
    if entity and _ENTITY_END.search(value):
        return EntityCountSpec(entity=entity, min_events=1)
    return None


def _common_len(left: str, right: str) -> int:
    best = 0
    for start in range(len(left)):
        for end in range(start + 2, len(left) + 1):
            part = left[start:end]
            if part in right:
                best = max(best, len(part))
    return best


@dataclass(frozen=True)
class EntityResolution:
    status: str
    selected: str | None = None
    candidates: tuple[tuple[str, str, int], ...] = ()
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "selected": self.selected,
            "reason": self.reason,
            "candidates": [
                {"metric_id": mid, "name": name, "score": score}
                for mid, name, score in self.candidates
            ],
        }


def resolve_entity_count_metric(
    spec: EntityCountSpec, sem: SemanticLayer,
    dimensions: tuple[str, ...] = (),
) -> EntityResolution:
    entity = str(spec.entity or "").strip()
    if not entity:
        return EntityResolution("missing", reason="没有识别出被计数的实体")
    scored: list[tuple[int, str, str]] = []
    for metric in sem.metrics.values():
        if metric.type != "entity_count":
            continue
        supported = set(metric.raw.get("supported_grain") or ["none"])
        if dimensions and any(dim not in supported for dim in dimensions):
            continue
        best = 0
        for term in [metric.name, *metric.synonyms]:
            term = str(term or "")
            if not term:
                continue
            if entity in term:
                best = max(best, 100 + len(entity))
            elif term in entity:
                best = max(best, 80 + len(term))
            else:
                best = max(best, _common_len(entity, term) * 10)
        if best > 0:
            scored.append((best, metric.id, metric.name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored:
        return EntityResolution(
            "missing",
            reason=f"当前语义层没有「{entity}」的实体计数口径",
        )
    top = scored[0]
    second = scored[1] if len(scored) > 1 else None
    if second is None or top[0] - second[0] >= 10:
        return EntityResolution(
            "unique", top[1],
            tuple((mid, name, score) for score, mid, name in scored[:5]),
            f"命中实体计数指标「{top[2]}」",
        )
    return EntityResolution(
        "ambiguous",
        candidates=tuple((mid, name, score) for score, mid, name in scored[:5]),
        reason="多个实体计数指标都可能匹配，需要澄清",
    )


def apply_entity_count(
    slots: dict[str, Any],
    text: str,
    sem: SemanticLayer,
    *,
    previous: dict[str, Any] | None = None,
    explicit: bool = False,
) -> dict[str, Any]:
    existing = slots.get("entity_count")
    detected = detect_entity_count(text)
    if isinstance(existing, EntityCountSpec):
        spec = existing
    elif isinstance(existing, dict):
        try:
            spec = EntityCountSpec.model_validate(existing)
        except Exception:
            spec = None
    else:
        spec = None
    auto = spec is None and detected is not None
    if spec is None:
        spec = detected
    if spec is None:
        return slots

    entity = str(spec.entity or "").strip()
    previous = previous or {}
    prev_spec = previous.get("entity_count")
    if not entity and isinstance(prev_spec, dict):
        entity = str(prev_spec.get("entity") or "").strip()
    if not entity:
        return slots

    resolved = EntityCountSpec(entity=entity, min_events=spec.min_events)
    dimensions = tuple(str(dim) for dim in (slots.get("dimensions") or []))
    resolution = resolve_entity_count_metric(resolved, sem, dimensions)
    if resolution.status == "unique" and resolution.selected:
        min_events = resolved.min_events
        if auto and min_events == 1:
            selected = sem.metrics.get(resolution.selected)
            default = int(selected.raw.get("min_orders") or 1) if selected else 1
            if default > 1:
                min_events = default
        if (
            auto and min_events == 1 and not explicit
            and not any(cue in entity for cue in ("自然", "去重", "唯一", "不同"))
        ):
            return slots
        bound = EntityCountSpec(entity=entity, min_events=min_events)
        slots["metric"] = resolution.selected
        slots["metric_source"] = "entity_count"
        slots["entity_count"] = bound.model_dump(mode="json")
        slots["nested"] = {
            "entity": entity,
            "min_orders": min_events,
        }
        slots["having"] = []
        slots["unresolved"] = [
            term for term in (slots.get("unresolved") or [])
            if entity not in str(term)
        ]
        slots["notes"] = list(dict.fromkeys(
            list(slots.get("notes") or [])
            + [f"实体计数：{entity}，每个实体至少 {min_events} 个事件"]
        ))
        return slots

    if auto and resolved.min_events <= 1 and not explicit:
        # A bare "how many customers" is ambiguous with the order-scoped
        # customer_id metric.  Only claim the natural-key count when the
        # question explicitly asks for a natural/deduplicated entity.
        if not any(cue in entity for cue in ("自然", "去重", "唯一", "不同")):
            return slots
    slots["entity_count"] = resolved.model_dump(mode="json")
    slots["metric"] = None
    slots["having"] = []
    slots["unresolved"] = [entity]
    slots["notes"] = list(dict.fromkeys(
        list(slots.get("notes") or []) + [resolution.reason]
    ))
    return slots
