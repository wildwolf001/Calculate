"""Templates for dimension-member listings.

The SQL lives in the compiler (`member_list` plan shape). This module only
builds the deterministic answer from the semantic dimension and optional
coverage configuration; it contains no business-specific labels.
"""
from __future__ import annotations

from app.domain.semantic.loader import SemanticLayer


def describe(sem: SemanticLayer, dim: str, rows: list[list], total: int) -> str:
    try:
        dimension = sem.dimension(dim)
        label = dimension.raw.get("name_zh", dim)
    except Exception:
        label = dim
    if not rows:
        return f"当前范围内没有可用的{label}。"
    names = [str(r[0]) for r in rows[:10]]
    more = "" if total <= 10 else f" 等 {total} 个"
    lines = [f"共有 **{total} 个{label}**：", "、".join(names) + more + "。"]
    coverage = sem.capabilities.get("member_list_coverage") or {}
    coverage_label = coverage.get("label")
    if coverage_label:
        lines.extend(["", f"每个{label}的{coverage_label}见右侧表格。"])
    if total > 10:
        lines.append("完整清单请在右侧表格查看或导出 CSV。")
    return "\n".join(lines)
