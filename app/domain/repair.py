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


def _preferred_metrics(sem: SemanticLayer) -> list:
    selected = [m for m in sem.metrics.values() if m.raw.get("selected", True)]
    return selected or list(sem.metrics.values())


def _filter_context(sem: SemanticLayer, filters: dict) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for dim_id, spec in filters.items():
        if dim_id not in sem.dimensions:
            continue
        value = _first_value(spec)
        if not value:
            continue
        label = sem.dimensions[dim_id].raw.get("name_zh", dim_id)
        out.append((dim_id, label, value))
    return out


def _column_options(model_card: dict | None, slots: dict,
                    requested, unknown: list[str] | None = None
                    ) -> list[str]:
    """When the unknown term is a field/column, offer concrete value columns."""
    if not model_card:
        return []
    terms = " ".join(str(item) for item in (unknown or []))
    all_labels = [
        str(column.get("label") or column.get("name") or "")
        for table in (model_card.get("tables") or {}).values()
        for column in table.get("columns") or []
    ]
    field_markers = (
        "字段", "列", "评分", "价格", "金额", "运费", "数量",
        "时长", "成本", "编号", "score", "price", "amount",
    )
    looks_like_field = any(marker in terms for marker in field_markers) or any(
        label and label in terms for label in all_labels
    )
    if not looks_like_field:
        return []
    fact = ""
    spec = slots.get("metric_spec")
    if isinstance(spec, dict):
        fact = str(spec.get("fact") or "")
    if not fact and requested is not None:
        fact = str((requested.raw or {}).get("fact") or "")
    tables = model_card.get("tables") or {}
    candidates = []
    if fact and fact in tables:
        candidates = [tables[fact]]
    else:
        candidates = [t for t in tables.values() if t.get("role") == "fact"][:2]
    options: list[str] = []
    for table in candidates:
        for column in table.get("columns") or []:
            if str(column.get("role")) != "measure":
                continue
            label = str(column.get("description") or column.get("label") or "")
            if not any("\u4e00" <= char <= "\u9fff" for char in label):
                # Do not offer English physical names as example questions.
                continue
            options.append(f"{label}的平均值是多少？")
            if len(options) >= 3:
                return options
    return options


def build_repair(sem: SemanticLayer, slots,
                 unknown: list[str] | None = None,
                 model_card: dict | None = None) -> dict:
    data = _as_dict(slots)
    filters = data.get("filters") or {}
    period = time_label(data.get("time"))
    requested = sem.metrics.get(data.get("metric") or "")
    selected = _preferred_metrics(sem)
    contexts = _filter_context(sem, filters)

    options: list[str] = []
    primary_dim = None
    primary_value = None
    for dim_id, _label, value in contexts:
        compatible = [m for m in selected if _supports(m, dim_id)]
        if compatible:
            primary_dim, primary_value = dim_id, value
            for metric in compatible[:3]:
                options.append(f"{value}的{metric.name}是多少？")
            break

    if requested and primary_dim and not _supports(requested, primary_dim):
        prefix = f"{period}的" if period else ""
        options.append(f"{prefix}{requested.name}是多少？")
    if not options:
        pool = ([requested] if requested else []) + [
            m for m in selected if m is not requested
        ]
        for metric in pool[:3]:
            prefix = f"{period}的" if period else ""
            options.append(f"{prefix}{metric.name}是多少？")

    options = list(dict.fromkeys(options))[:4]
    column_options = _column_options(model_card, data, requested, unknown)
    if column_options:
        options = list(dict.fromkeys(column_options + options))[:4]

    if unknown:
        note = "它可能是业务概念、计算口径或外部数据，不能直接查询。"
    else:
        note = "当前口径下这个指标不能按所问维度计算。"

    clarification = None
    if primary_dim and requested and not _supports(requested, primary_dim):
        compatible = [m for m in selected if _supports(m, primary_dim)]
        if compatible:
            prefix = f"{period}的" if period else ""
            clarification = (
                f"你是想看「{primary_value}」的{compatible[0].name}，"
                f"还是「{prefix}全部数据」的{requested.name}？"
            )
    elif primary_dim:
        clarification = f"你想先看「{primary_value}」的哪项指标？"

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
