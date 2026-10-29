"""Compact schema/value card used by the parser prompt.

Everything here is derived from the current dataset's semantic layer and
value dictionary. There is no built-in e-commerce vocabulary.
"""
from __future__ import annotations

import json

from app.config.settings import DIM_VALUES_PATH
from app.domain.semantic.graph import build_capabilities
from app.domain.semantic.loader import SemanticLayer


def build_schema_card(sem: SemanticLayer, max_values: int = 12,
                      dim_values_path=DIM_VALUES_PATH) -> str:
    metrics = [
        m for m in sem.metrics.values()
        if m.raw.get("selected", True)
    ] or list(sem.metrics.values())
    dimensions = [
        d for d in sem.dimensions.values()
        if d.raw.get("selected", True)
    ] or list(sem.dimensions.values())

    lines: list[str] = []
    lines.append(f"数据时间范围: {sem.time_range[0]} ~ {sem.time_range[1]}（右开）")
    lines.append("\n指标（metric_id: 中文名; 同义词; 支持维度; 口径）:")
    capabilities = build_capabilities(sem)
    for m in metrics:
        capability = capabilities.get(m.id)
        grains = (
            sorted(capability.allowed_dimensions - {"none", "month"})
            if capability is not None
            else [g for g in m.raw.get("supported_grain", []) if g != "none"]
        )
        axis = (
            f"; 时间轴={capability.time_axis}"
            if capability is not None and capability.time_axis else ""
        )
        lines.append(
            f"- {m.id}: {m.name}; 同义词={m.synonyms}; 支持维度={grains}{axis}; "
            f"口径={m.raw.get('definition', '')}"
        )
    lines.append("\n维度（dimension_id: 中文名; 同义词）:")
    for d in dimensions:
        lines.append(
            f"- {d.id}: {d.raw.get('name_zh', d.id)}; {d.raw.get('synonyms', [])}"
        )
    if getattr(sem, "time_axes", None):
        lines.append("\n时间轴（time_axis: ref = 含义）:")
        for axis in sem.time_axes[:12]:
            lines.append(
                f"- {axis.get('ref')}: {axis.get('name_zh')}（{axis.get('label')}）"
            )

    try:
        data = json.loads(dim_values_path.read_text(encoding="utf-8"))
        values = data.get("values", {})
        lines.append("\n维度取值样例（必须使用真实值，不要编造）:")
        for dim in dimensions:
            if dim.kind == "time":
                continue
            key = dim.raw.get("filter_lookup") or dim.raw.get("column")
            vals = values.get(key, []) if key else []
            if vals:
                sample = ", ".join(map(str, vals[:max_values]))
                more = f" …共{len(vals)}个" if len(vals) > max_values else ""
                lines.append(f"- {dim.id}: {sample}{more}")
    except Exception:
        pass

    capabilities = sem.capabilities or {}
    lines.append("\n规则:")
    draft = sem.raw.get("status", "published") == "draft"
    if draft:
        lines.append("- 以上口径为草稿/自动推断，尚未经业务确认；不得自行补充业务过滤条件。")
    else:
        lines.append("- 以上口径已发布；仍不得编造未列出的指标、维度或取值。")
    lines.append("- 只能使用上面列出的 metric_id / dimension_id；未知项放入 unsupported。")
    lines.append("- 过滤值必须来自维度取值样例，不要编造。")
    lines.append("- “多少种/几种/多少个”是去重计数问题，优先选择计数指标，不要当成员清单。")
    if capabilities.get("member_list", True):
        lines.append("- “有哪些/列出/清单”可以使用 lookup；成员清单只返回真实维度值。")
    else:
        lines.append("- 当前数据源不支持成员清单；清单类问题请引导到计数或分组查询。")
    if capabilities.get("ratio", True):
        lines.append("- 比率指标按声明的分子/分母计算，不要自行相除。")
    lines.append("- 时间范围以数据实际覆盖为准；未写年份时按数据中最近一次出现理解。")
    for rule in sem.raw.get("prompt_rules") or []:
        lines.append(f"- {rule}")
    return "\n".join(lines)
