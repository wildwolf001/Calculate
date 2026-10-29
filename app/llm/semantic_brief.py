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


def _score(item: dict[str, Any], question: str) -> int:
    """Chinese-aware retrieval: match names/synonyms/labels as substrings.

    Splitting on whitespace used to give every item score 0 for a Chinese
    question, so the brief always showed the alphabetically first metrics.
    """
    text = str(question or "")
    name = str(item.get("name") or "")
    score = 5 if name and name in text else 0
    for synonym in item.get("synonyms") or []:
        if synonym and str(synonym) in text:
            score += 3
    for label in (item.get("labels") or {}).values():
        if label and str(label) in text:
            score += 2
    for key in ("definition", "expr", "numerator", "denominator"):
        value = item.get(key)
        if value and str(value) in text:
            score += 1
    return score


def _metric_line(metric_id: str, metric: dict[str, Any],
                 dim_names: dict[str, str]) -> str:
    expr = metric.get("expr") or (
        f'{metric.get("numerator") or ""} / {metric.get("denominator") or ""}'
    )
    scope = metric.get("scope") or {}
    dims = metric.get("supported_dimensions") or []
    dim_text = [
        f"{dim_id}({dim_names.get(dim_id, dim_id)})"
        for dim_id in dims[:12]
    ]
    return (
        f"- {metric_id}: {metric.get('name')}; "
        f"type={metric.get('type')}; expr={expr}; "
        f"fact={metric.get('fact')}; scope={scope}; "
        f"time_via={metric.get('time_via')}; unit={metric.get('unit')}; "
        f"dims={dim_text}"
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
    metrics = index.get("metrics") or {}
    dimensions = index.get("dimensions") or {}
    dim_names = {
        dim_id: str(dim.get("name") or dim_id)
        for dim_id, dim in dimensions.items()
    }

    ranked_metrics = sorted(
        metrics.items(),
        key=lambda item: (-_score(item[1], question), str(item[0])),
    )[:max_metrics]
    ranked_dims = sorted(
        dimensions.items(),
        key=lambda item: (-_score(item[1], question), str(item[0])),
    )[:max_dimensions]

    lines: list[str] = []
    lines.append(f"dataset={index.get('dataset_id')} semantic_version={index.get('semantic_version')}")
    lines.append("指标:")
    lines.extend(
        _metric_line(mid, metric, dim_names) for mid, metric in ranked_metrics
    )
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
