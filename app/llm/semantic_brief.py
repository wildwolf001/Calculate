"""Build a compact semantic brief from the index.

The brief replaces the old 12k-char schema card: it retrieves a handful of
metrics/dimensions relevant to the question and includes their expressions,
scope and real values. The chat agent sees this, not the whole dataset.
"""
from __future__ import annotations

import re
from typing import Any

MAX_CHARS = 6000


def _tokens(text: str) -> list[str]:
    return [part for part in re.split(r"[\s,，。；;:：/、()（）]+", text or "") if part]


def _score(item: dict[str, Any], tokens: list[str]) -> int:
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("name", "definition", "expr", "numerator", "denominator")
    )
    haystack += " " + " ".join(str(v) for v in item.get("synonyms") or [])
    score = 0
    for token in tokens:
        if token and token in haystack:
            score += 1
    return score


def _metric_line(metric_id: str, metric: dict[str, Any]) -> str:
    expr = metric.get("expr") or (
        f'{metric.get("numerator") or ""} / {metric.get("denominator") or ""}'
    )
    scope = metric.get("scope") or {}
    dims = metric.get("supported_dimensions") or []
    return (
        f"- {metric_id}: {metric.get('name')}; "
        f"type={metric.get('type')}; expr={expr}; "
        f"fact={metric.get('fact')}; scope={scope}; "
        f"time_via={metric.get('time_via')}; unit={metric.get('unit')}; "
        f"dims={dims[:12]}"
    )


def _dimension_line(dim_id: str, dim: dict[str, Any]) -> str:
    values = dim.get("values") or []
    sample = ", ".join(str(v) for v in values[:8])
    return (
        f"- {dim_id}: {dim.get('name')}; kind={dim.get('kind')}; "
        f"expr={dim.get('expr') or dim.get('column')}; "
        f"labels={dim.get('labels') or {}}; values=[{sample}]"
    )


def build_brief(index: dict[str, Any], question: str,
                max_metrics: int = 8, max_dimensions: int = 8,
                max_chars: int = MAX_CHARS) -> str:
    if not index:
        return ""
    tokens = _tokens(question)
    metrics = index.get("metrics") or {}
    dimensions = index.get("dimensions") or {}

    ranked_metrics = sorted(
        metrics.items(),
        key=lambda item: (-_score(item[1], tokens), str(item[0])),
    )[:max_metrics]
    ranked_dims = sorted(
        dimensions.items(),
        key=lambda item: (-_score(item[1], tokens), str(item[0])),
    )[:max_dimensions]

    lines: list[str] = []
    lines.append(f"dataset={index.get('dataset_id')} semantic_version={index.get('semantic_version')}")
    lines.append("指标:")
    lines.extend(_metric_line(mid, metric) for mid, metric in ranked_metrics)
    lines.append("维度:")
    lines.extend(_dimension_line(did, dim) for did, dim in ranked_dims)

    tables = index.get("tables") or {}
    lines.append("表:")
    for table_id, table in list(tables.items())[:8]:
        columns = [
            f"{col.get('name')}:{col.get('type')}"
            for col in (table.get("columns") or [])[:12]
        ]
        lines.append(
            f"- {table_id}: rows={table.get('rows')}; grain={table.get('grain')}; "
            f"time={table.get('time')}; columns={columns}"
        )

    docs = index.get("docs") or []
    if docs:
        lines.append("口径文档:")
        for doc in docs[:8]:
            lines.append(
                f"- {doc.get('name')}: {doc.get('definition')} "
                f"formula={doc.get('formula')}"
            )

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
    return text
