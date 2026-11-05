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


def _longest_common(term: str, text: str) -> int:
    """Longest common substring length (Chinese has no word spaces)."""
    best = 0
    for start in range(len(term)):
        for end in range(start + 2, len(term) + 1):
            part = term[start:end]
            if part in text:
                best = max(best, len(part))
    return best


def _term_score(term: Any, text: str, exact: int, partial: int) -> int:
    value = str(term or "")
    if not value:
        return 0
    if value in text:
        return exact
    length = _longest_common(value, text)
    return min(length, partial) if length >= 2 else 0


def _score(item: dict[str, Any], question: str) -> int:
    """Chinese-aware retrieval: match names/synonyms/labels as substrings.

    Exact matching alone missed "延迟率" because the published metric is
    "配送延迟率"; partial substring matching fixes that class of miss.
    """
    text = str(question or "")
    score = _term_score(item.get("name"), text, exact=10, partial=6)
    for synonym in item.get("synonyms") or []:
        score += _term_score(synonym, text, exact=6, partial=3)
    for label in (item.get("labels") or {}).values():
        score += _term_score(label, text, exact=4, partial=2)
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
    # Recall catalogue: names are cheap and must never be lost to top-k
    # truncation. Expressions/values below are still retrieved by relevance.
    metric_catalog = list(metrics.items())[:120]
    lines.append("指标目录:")
    lines.append("; ".join(
        f"{mid}={metric.get('name') or mid}" for mid, metric in metric_catalog
    ))
    dim_catalog = list(dimensions.items())[:120]
    lines.append("维度目录:")
    lines.append("; ".join(
        f"{did}={dim.get('name') or did}" for did, dim in dim_catalog
    ))
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
