"""Build and read the query-time semantic index.

The index is the single read model the chat agent sees: tables/columns,
relations, metric expressions, dimension values and documentation rules.
It is generated at publish time from the semantic layer + catalog + dim values;
it never contains SQL and never writes to the warehouse.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.domain.semantic.loader import SemanticLayer

INDEX_VERSION = 1
MAX_DIM_VALUES = 50


def semantic_fingerprint(sem: SemanticLayer) -> str:
    """Content hash of the semantic layer.

    `version` only changes on publish, but a draft edit can rename metrics or
    change synonyms without bumping it. The fingerprint lets a loaded context
    detect that its on-disk index no longer describes the YAML it just read.
    Hash the effective layer (not the raw YAML text) so whitespace/order edits
    do not invalidate the index.
    """
    payload = json.dumps(
        {
            "version": sem.version,
            "tables": sem.tables,
            "joins": sem.joins,
            "time_axes": sem.time_axes,
            "time_range": list(sem.time_range),
            "metrics": {mid: metric.raw for mid, metric in sem.metrics.items()},
            "dimensions": {did: dim.raw for did, dim in sem.dimensions.items()},
            "entities": {eid: ent.raw for eid, ent in sem.entities.items()},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dimension_values(raw: dict[str, Any], dim_values: dict[str, Any]) -> list[str]:
    key = raw.get("filter_lookup") or raw.get("column")
    if not key:
        return []
    values = (dim_values.get("values") or {}).get(key) or []
    out: list[str] = []
    for value in values:
        text = str(value)
        if text not in out:
            out.append(text)
        if len(out) >= MAX_DIM_VALUES:
            break
    return out


def _dimension_is_pii(raw: dict[str, Any], catalog: dict[str, Any]) -> bool:
    ref = str(raw.get("column") or raw.get("filter_column") or "")
    table, _, column = ref.partition(".")
    if not table or not column:
        return False
    for meta in ((catalog.get("tables") or {}).get(table) or {}).get("columns") or []:
        if str(meta.get("name")) == column:
            return bool(meta.get("pii"))
    return False


def _columns(raw: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    table = (catalog.get("tables") or {}).get(raw.get("id") or "") or {}
    columns = []
    for column in table.get("columns") or []:
        columns.append({
            "name": column.get("name"),
            "label": column.get("label"),
            "type": column.get("type"),
            "distinct": column.get("distinct"),
            "null_rate": column.get("null_rate"),
            "pii": bool(column.get("pii")),
            "samples": [] if column.get("pii") else (column.get("samples") or []),
        })
    return columns


def build_index(
    *,
    dataset_id: str,
    data_version: str,
    semantic_version: int,
    sem: SemanticLayer,
    catalog: dict[str, Any] | None = None,
    dim_values: dict[str, Any] | None = None,
    documentation: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = catalog or {}
    dim_values = dim_values or {}
    capabilities = capabilities or {}

    tables: dict[str, Any] = {}
    for table_id, raw in sem.tables.items():
        meta = (catalog.get("tables") or {}).get(table_id) or {}
        tables[table_id] = {
            "grain": list(raw.get("grain") or []),
            "time": raw.get("time"),
            "rows": meta.get("rows"),
            "columns": _columns({"id": table_id}, catalog),
        }

    metrics: dict[str, Any] = {}
    for metric_id, metric in sem.metrics.items():
        raw = metric.raw
        cap = capabilities.get(metric_id) or {}
        metrics[metric_id] = {
            "name": metric.name,
            "type": metric.type,
            "fact": raw.get("fact"),
            "expr": raw.get("expr"),
            "numerator": raw.get("numerator"),
            "denominator": raw.get("denominator"),
            "entity": raw.get("entity"),
            "entity_join": raw.get("entity_join"),
            "min_orders": raw.get("min_orders"),
            "count_orders": raw.get("count_orders"),
            "scope": dict(raw.get("scope") or {}),
            "time_via": raw.get("time_via"),
            "time_axis": raw.get("time_axis"),
            "unit": raw.get("unit"),
            "supported_dimensions": sorted(
                cap.get("allowed_dimensions") or raw.get("supported_grain") or []
            ),
            "definition": raw.get("definition"),
            "provenance": raw.get("provenance") or "unknown",
            "confidence": raw.get("confidence"),
        }

    dimensions: dict[str, Any] = {}
    for dim_id, dim in sem.dimensions.items():
        raw = dim.raw
        pii = _dimension_is_pii(raw, catalog)
        dimensions[dim_id] = {
            "name": raw.get("name_zh") or dim_id,
            "kind": dim.kind,
            "column": raw.get("column"),
            "expr": raw.get("expr"),
            "requires": raw.get("requires"),
            "labels": dict(raw.get("labels") or {}),
            "synonyms": list(raw.get("synonyms") or []),
            # PII values never leave the local warehouse, not even into a
            # prompt: the model only needs the dimension's existence here.
            "pii": pii,
            "values": [] if pii else _dimension_values(raw, dim_values),
        }

    relations: list[dict[str, Any]] = []
    for source, parents in sem.joins.items():
        for target, rel in (parents or {}).items():
            relations.append({
                "from": source,
                "to": target,
                "left": rel.get("left"),
                "right": rel.get("right"),
                "card": rel.get("card"),
            })

    docs: list[dict[str, Any]] = []
    for rule in (documentation or {}).get("rules") or []:
        if not isinstance(rule, dict):
            continue
        docs.append({
            "name": rule.get("name"),
            "definition": rule.get("definition"),
            "formula": rule.get("formula"),
            "table": rule.get("table_id") or rule.get("table"),
            "columns": list(rule.get("resolved_columns") or []),
        })

    return {
        "version": INDEX_VERSION,
        "dataset_id": dataset_id,
        "data_version": data_version,
        "semantic_version": int(semantic_version),
        "semantic_fingerprint": semantic_fingerprint(sem),
        "tables": tables,
        "relations": sorted(relations, key=lambda item: (item["from"], item["to"])),
        "metrics": metrics,
        "dimensions": dimensions,
        "docs": docs,
        "time_axes": list(sem.time_axes),
        "issues": [],
    }


def build_index_for_context(ctx: Any) -> dict[str, Any]:
    return build_index(
        dataset_id=ctx.dataset_id,
        data_version=ctx.data_version,
        semantic_version=ctx.sem.version,
        sem=ctx.sem,
        catalog=ctx.catalog,
        dim_values=ctx.dim_values,
        documentation=ctx.documentation,
        capabilities=(ctx.capabilities or {}).get("metrics") or {},
    )


def write_index(path: Path, index: dict[str, Any]) -> None:
    text = json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_index(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not data:
        return {}
    if data.get("version") != INDEX_VERSION:
        return {}
    return data


def validate_index(index: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(index, dict):
        return ["index 不是对象"]
    for key in ("version", "dataset_id", "tables", "metrics", "dimensions"):
        if key not in index:
            errors.append(f"缺少 {key}")
    if index.get("version") != INDEX_VERSION:
        errors.append(f"version 必须是 {INDEX_VERSION}")
    for metric_id, metric in (index.get("metrics") or {}).items():
        if not isinstance(metric, dict):
            errors.append(f"metric {metric_id} 不是对象")
            continue
        if not metric.get("fact"):
            errors.append(f"metric {metric_id} 缺少 fact")
    return errors
