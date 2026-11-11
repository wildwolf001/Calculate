"""Build a compact, schema-first semantic brief from a model card.

The brief is the only schema text the intent parser sees. Its job is to make
the *relevant* tables/columns unmistakable: a review question must see
``t_006.c03 review_score integer measure`` even if the dataset has many tables.
That is why the old "put everything, truncate the tail" approach was replaced
by this projection:

- pick the tables touched by the ranked metrics/dimensions (plus their N:1
  parents), not the first N tables;
- print the schema before the long catalogues, in a compact one-line-per-column
  form, and never tail-truncate it;
- give every section its own budget, and say explicitly what was omitted;
- keep dimension values out unless the question actually mentions a value
  (grounding fetches the rest on demand).
"""
from __future__ import annotations

import re
from typing import Any

MAX_CHARS = 6000
MAX_TABLES = 8
SCHEMA_BUDGET = 3400
METRIC_BUDGET = 800
DIM_BUDGET = 800
REL_BUDGET = 300
DOC_BUDGET = 250
MAX_METRICS = 8
MAX_DIMENSIONS = 12


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
    """Chinese-aware retrieval: match names/synonyms/labels as substrings."""
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


# ---------------------------------------------------------------------------
# source normalisation (model card or legacy index)
# ---------------------------------------------------------------------------

def _items(source: dict[str, Any], key: str) -> list[tuple[str, dict[str, Any]]]:
    value = source.get(key) or {}
    if isinstance(value, dict):
        return [(str(k), v) for k, v in value.items() if isinstance(v, dict)]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append((str(item.get("id") or item.get("name") or ""), item))
        return [item for item in out if item[0]]
    return []


def _is_card(source: dict[str, Any]) -> bool:
    for table in (source.get("tables") or {}).values():
        for column in (table or {}).get("columns") or []:
            if "role" in column:
                return True
    return isinstance(source.get("metrics"), list)


def _relations(source: dict[str, Any]) -> list[dict[str, Any]]:
    relations = source.get("relations") or []
    if isinstance(relations, dict):
        out = []
        for child, parents in relations.items():
            for parent, rel in (parents or {}).items():
                out.append({
                    "from": f"{child}.{rel.get('left') or rel.get('on') or ''}",
                    "to": f"{parent}.{rel.get('right') or rel.get('on') or ''}",
                    "card": rel.get("card") or "N:1",
                })
        return out
    return [item for item in relations if isinstance(item, dict)]


def _table_of(ref: Any) -> str:
    text = str(ref or "").strip()
    if "." in text:
        return text.split(".", 1)[0]
    return text


def _pick_tables(source: dict[str, Any], question: str,
                 ranked_metrics: list[tuple[str, dict[str, Any]]],
                 ranked_dims: list[tuple[str, dict[str, Any]]]) -> list[str]:
    tables = source.get("tables") or {}
    if not tables:
        return []
    # Small databases fit whole: showing every table is more reliable than
    # guessing relevance from Chinese question words (e.g. "评分" does not
    # substring-match an English column label "review_score").
    all_tables = list(tables.keys())
    try:
        if len("\n".join(_schema_lines(source, all_tables, 15))) <= SCHEMA_BUDGET:
            return all_tables
    except Exception:
        pass
    seeds: list[str] = []
    for _mid, metric in ranked_metrics[:6]:
        fact = _table_of(metric.get("fact"))
        if fact in tables and fact not in seeds:
            seeds.append(fact)
    for _did, dim in ranked_dims[:6]:
        ref = dim.get("column") or dim.get("expr") or ""
        table = _table_of(ref)
        if table in tables and table not in seeds:
            seeds.append(table)
    if not seeds:
        # No semantic hit: keep the first few tables (stable, explainable).
        return list(tables.keys())[:4]

    edges = _relations(source)
    parents: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    for relation in edges:
        child = _table_of(relation.get("from"))
        parent = _table_of(relation.get("to"))
        if not child or not parent:
            continue
        parents.setdefault(child, []).append(parent)
        children.setdefault(parent, []).append(child)

    selected: list[str] = []

    def add(table: str) -> None:
        if table in tables and table not in selected and len(selected) < MAX_TABLES:
            selected.append(table)

    # Walk seeds in relevance order and pull in their direct N:1 parents and
    # children immediately. Children matter: a review question ranks order
    # metrics first, but the review table is a *child* of orders and must be
    # visible before unrelated facts consume the budget.
    for seed in seeds:
        add(seed)
        for parent in parents.get(seed, []):
            add(parent)
        for child in children.get(seed, []):
            add(child)
        if len(selected) >= MAX_TABLES:
            break
    # If there is still room, expand one hop from what was selected.
    for table in list(selected):
        for parent in parents.get(table, []):
            add(parent)
        if len(selected) >= MAX_TABLES:
            break
    return selected[:MAX_TABLES]


# ---------------------------------------------------------------------------
# section rendering
# ---------------------------------------------------------------------------

def _metric_line(metric_id: str, metric: dict[str, Any]) -> str:
    aggregation = metric.get("aggregation")
    operand = metric.get("operand")
    if aggregation and operand:
        core = f"{aggregation}({operand})"
    elif metric.get("type") == "ratio":
        core = f"{metric.get('numerator') or '?'} / {metric.get('denominator') or '?'}"
    elif metric.get("type") == "entity_count":
        core = f"entity={metric.get('entity') or metric.get('fact')}"
    else:
        core = metric.get("expr") or ""
    parts = [
        f"{metric_id} {metric.get('name') or metric_id}",
        str(metric.get("type") or "aggregate"),
        core,
        f"fact={metric.get('fact')}",
    ]
    if metric.get("time_via"):
        parts.append(f"time={metric.get('time_via')}")
    if metric.get("scope"):
        parts.append(f"scope={metric.get('scope')}")
    if metric.get("unit"):
        parts.append(f"unit={metric.get('unit')}")
    return " | ".join(str(part) for part in parts if part)


def _dimension_line(dimension_id: str, dim: dict[str, Any], question: str) -> str:
    parts = [
        f"{dimension_id} {dim.get('name') or dimension_id}",
        str(dim.get("kind") or "categorical"),
        str(dim.get("column") or dim.get("expr") or ""),
    ]
    if dim.get("labels"):
        parts.append(f"labels={dim.get('labels')}")
    text = str(question or "")
    matched = _score(dim, question) > 0
    values = dim.get("values") or []
    if matched and values:
        parts.append("values=[" + ", ".join(str(v) for v in values[:5]) + "]")
    return " | ".join(str(part) for part in parts if part)


def _column_line(column: dict[str, Any]) -> str:
    name = str(column.get("name") or "")
    label = str(column.get("label") or name)
    role = str(column.get("role") or "")
    column_type = str(column.get("type") or "")
    bits = [name]
    if label and label != name:
        bits.append(label)
    bits.append(column_type)
    if role:
        bits.append(role)
    if column.get("fk"):
        bits.append(f"fk={column.get('fk')}")
    if column.get("unit"):
        bits.append(f"unit={column.get('unit')}")
    if column.get("min") is not None or column.get("max") is not None:
        bits.append(f"range={column.get('min')}..{column.get('max')}")
    return " ".join(bit for bit in bits if bit)


def _schema_lines(source: dict[str, Any], selected: list[str],
                  max_columns: int = 15) -> list[str]:
    tables = source.get("tables") or {}
    lines: list[str] = []
    for table_id in selected:
        table = tables.get(table_id) or {}
        header = [
            f"- {table_id} {table.get('label') or ''}".strip(),
            f"rows={table.get('rows')}",
            f"grain={table.get('grain') or table.get('primary_key') or []}",
        ]
        if table.get("time"):
            header.append(f"time={table.get('time')}")
        lines.append(" | ".join(str(part) for part in header if part))
        columns = list(table.get("columns") or [])
        # Keys/measures/time first, dimensions last; never hide the columns
        # that identify or measure a row.
        order = {"pk": 0, "fk": 0, "time": 1, "measure": 2, "dimension": 3}
        columns.sort(key=lambda col: order.get(str(col.get("role")), 4))
        shown = columns[:max_columns]
        for column in shown:
            if _is_card(source):
                lines.append("  " + _column_line(column))
            else:
                lines.append(
                    "  " + f"{column.get('name')}({column.get('label')}):{column.get('type')}"
                )
        if len(columns) > len(shown):
            lines.append(f"  …另有 {len(columns) - len(shown)} 列（schema.search 查询）")
    return lines


def _clip(lines: list[str], budget: int, note: str) -> list[str]:
    if not lines:
        return []
    out: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > budget and out:
            out.append(note.format(n=len(lines) - len(out)))
            return out
        out.append(line)
        used += cost
    return out


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def build_brief(source: dict[str, Any], question: str,
                max_metrics: int = MAX_METRICS,
                max_dimensions: int = MAX_DIMENSIONS,
                max_chars: int = MAX_CHARS) -> str:
    """Project a model card (or legacy index) into the parser's schema text."""
    if not source:
        return ""
    card = _is_card(source)
    metrics = _items(source, "metrics")
    dimensions = _items(source, "dimensions")
    ranked_metrics = sorted(
        metrics, key=lambda item: (-_score(item[1], question), item[0])
    )[:max_metrics]
    ranked_dims = sorted(
        dimensions, key=lambda item: (-_score(item[1], question), item[0])
    )[:max_dimensions]
    # A time dimension (month) has no column and often scores zero for natural
    # phrases such as “近几个月”; still expose it so the LLM can choose a
    # monthly plan when the question asks for a trend. This is schema
    # availability, not a keyword-to-group-by rule.
    ranked_ids = {did for did, _ in ranked_dims}
    for item in dimensions:
        did, dim = item
        if did not in ranked_ids and str(dim.get("kind") or "") == "time":
            ranked_dims.append(item)
            ranked_ids.add(did)

    selected_tables = _pick_tables(source, question, ranked_metrics, ranked_dims)
    lines: list[str] = []
    lines.append(
        f"[dataset] {source.get('dataset_id')} semantic_version="
        f"{source.get('semantic_version')} tables={len(source.get('tables') or {})}"
    )

    # Schema first: this is the block that must never be truncated away.
    schema_budget = SCHEMA_BUDGET if max_chars >= 5000 else max(1200, max_chars - 2600)
    schema_lines: list[str] = []
    for per_table in (15, 10, 6, 3):
        schema_lines = _schema_lines(source, selected_tables, per_table)
        if len("\n".join(schema_lines)) <= schema_budget:
            break
    if len("\n".join(schema_lines)) > schema_budget:
        # Drop the least relevant tables, never the first (metric fact) one.
        while len(selected_tables) > 1:
            selected_tables = selected_tables[:-1]
            schema_lines = _schema_lines(source, selected_tables, 6)
            if len("\n".join(schema_lines)) <= schema_budget:
                break
    omitted = len(source.get("tables") or {}) - len(selected_tables)
    lines.append("[schema]")
    lines.extend(schema_lines)
    if omitted > 0:
        lines.append(f"…另有 {omitted} 张表未展示（schema.search 查询）")

    lines.append("[metrics]")
    metric_lines = [_metric_line(mid, item) for mid, item in ranked_metrics]
    lines.extend(_clip(metric_lines, METRIC_BUDGET, "…另有 {n} 个指标未展示"))
    if not metric_lines:
        lines.append("（无匹配指标）")

    lines.append("[dimensions]")
    dim_lines = [_dimension_line(did, item, question) for did, item in ranked_dims]
    lines.extend(_clip(dim_lines, DIM_BUDGET, "…另有 {n} 个维度未展示"))
    if not dim_lines:
        lines.append("（无匹配维度）")

    relations = [
        relation for relation in _relations(source)
        if _table_of(relation.get("from")) in selected_tables
        and _table_of(relation.get("to")) in selected_tables
    ]
    if relations:
        lines.append("[relations]")
        rel_lines = [
            f"{relation.get('from')} -> {relation.get('to')} {relation.get('card')}"
            for relation in relations
        ]
        lines.extend(_clip(rel_lines, REL_BUDGET, "…另有 {n} 条关系未展示"))

    docs = [doc for doc in (source.get("docs") or []) if isinstance(doc, dict)]
    if docs:
        lines.append("[docs]")
        doc_lines = [
            f"{doc.get('name')}: {doc.get('definition') or ''} "
            f"formula={doc.get('formula') or ''}"
            for doc in docs[:4]
        ]
        lines.extend(_clip(doc_lines, DOC_BUDGET, "…另有 {n} 条口径未展示"))

    text = "\n".join(lines)
    if len(text) > max_chars:
        # Last resort: the sections already have budgets; keep the schema and
        # metrics, drop docs/dimensions explicitly instead of tail-cutting.
        kept: list[str] = []
        used = 0
        for line in lines:
            cost = len(line) + 1
            if used + cost > max_chars:
                break
            kept.append(line)
            used += cost
        text = "\n".join(kept + ["…（预算不足，已省略后续段落）"])
    return text
