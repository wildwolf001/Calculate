"""Deterministic repair hints for refusals.

When a question cannot be answered, the user should not be left with only a
reason. This module derives what *can* be asked from the current slots and the
semantic layer, producing concrete questions (used as clickable chips) instead
of a generic metric list.
"""
from __future__ import annotations

from datetime import date

from app.domain.semantic.loader import SemanticLayer


def _as_dict(slots) -> dict:
    if slots is None:
        return {}
    if hasattr(slots, "model_dump"):
        return slots.model_dump(mode="json")
    return dict(slots)


def _first_value(spec) -> str | None:
    if spec is None:
        return None
    value = spec.get("value") if isinstance(spec, dict) else spec
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value is not None else None


def time_label(t) -> str:
    if not t:
        return ""
    if isinstance(t, (tuple, list)) and len(t) == 2:
        t = {"start": str(t[0]), "end": str(t[1])}
    if not isinstance(t, dict):
        return ""
    start, end = t.get("start"), t.get("end")
    if not start or not end:
        return ""
    try:
        sd, ed = date.fromisoformat(start), date.fromisoformat(end)
        months = (ed.year - sd.year) * 12 + (ed.month - sd.month)
        if sd.day == 1 and ed.day == 1 and months == 1:
            return f"{sd.year}年{sd.month}月"
    except ValueError:
        pass
    return f"{start} 至 {end}"


def _aggregate_metrics(sem: SemanticLayer) -> list:
    return [m for m in sem.metrics.values() if m.raw.get("type") == "aggregate"]


def _supports(metric, dim: str) -> bool:
    return dim in (metric.raw.get("supported_grain") or [])


def build_repair(sem: SemanticLayer, slots,
                 unknown: list[str] | None = None) -> dict:
    data = _as_dict(slots)
    filters = data.get("filters") or {}
    product = _first_value(filters.get("product"))
    period = time_label(data.get("time"))
    requested = sem.metrics.get(data.get("metric") or "")

    product_metrics = [m for m in _aggregate_metrics(sem) if _supports(m, "product")]
    options: list[str] = []
    if product:
        for m in product_metrics[:3]:
            options.append(f"{product}的{m.name}是多少？")
        if requested and not _supports(requested, "product"):
            scope = f"{period}全部已送达订单" if period else "全部已送达订单"
            options.append(f"{scope}的{requested.name}是多少？")
    else:
        pool = []
        if requested:
            pool.append(requested)
        for mid in ("gmv", "item_count", "order_count"):
            m = sem.metrics.get(mid)
            if m and m not in pool:
                pool.append(m)
        for m in pool[:3]:
            prefix = f"{period}的" if period else ""
            options.append(f"{prefix}{m.name}是多少？")

    options = list(dict.fromkeys(options))[:4]

    if unknown:
        terms = "、".join(unknown)
        note = f"数据里没有「{terms}」这个指标或字段。"
    else:
        note = "当前口径下这个指标不能按所问维度计算。"

    clarification = None
    if product and requested and not _supports(requested, "product") and product_metrics:
        scope = f"{period}全部已送达订单" if period else "全部已送达订单"
        clarification = (
            f"你是想看「{product}」的{product_metrics[0].name}，"
            f"还是「{scope}」的{requested.name}？"
        )
    elif product:
        clarification = f"你想先看「{product}」的哪项指标？"

    return {
        "note": note,
        "options": options,
        "clarification": clarification,
    }


def repair_text(repair: dict) -> str:
    lines = [repair["note"]]
    if repair.get("clarification"):
        lines.append(repair["clarification"])
    if repair.get("options"):
        lines.append("可以这样问：")
        lines.extend(f"- {q}" for q in repair["options"])
    return "\n\n".join(lines)
