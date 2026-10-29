"""Preserve human-confirmed semantics across a dataset rebuild.

A rebuild re-parses the raw files and regenerates the semantic draft. Without
this merge, every rebuild silently discards confirmed metrics, documented
definitions and derived dimensions. The merge is deliberately conservative:
an old object is restored only when every table/column it references still
exists in the new catalog.
"""
from __future__ import annotations

import json
import re
from typing import Any

REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z0-9_]+)\b")
KEEP_PROVENANCE = {"human", "documentation"}
METRIC_REF_FIELDS = (
    "expr", "numerator", "denominator", "grain_numerator", "grain_denominator",
    "count_orders", "entity", "time_via", "scope", "filters",
)


def _refs(value: Any) -> set[tuple[str, str]]:
    if isinstance(value, str):
        return set(REF_RE.findall(value))
    if isinstance(value, dict):
        out: set[tuple[str, str]] = set()
        for item in value.values():
            out |= _refs(item)
        return out
    if isinstance(value, (list, tuple)):
        out = set()
        for item in value:
            out |= _refs(item)
        return out
    return set()


def _columns(catalog: dict) -> dict[str, set[str]]:
    return {
        table_id: {str(col.get("name")) for col in (spec.get("columns") or [])}
        for table_id, spec in (catalog.get("tables") or {}).items()
    }


def _refs_exist(refs: set[tuple[str, str]], columns: dict[str, set[str]]) -> bool:
    return all(table in columns and column in columns[table] for table, column in refs)


def merge_semantics(new: dict, old: dict, catalog: dict) -> tuple[dict, list[str]]:
    columns = _columns(catalog)
    warnings: list[str] = []

    for metric_id, metric in (old.get("metrics") or {}).items():
        if metric.get("provenance") not in KEEP_PROVENANCE:
            continue
        refs = _refs({key: metric.get(key) for key in METRIC_REF_FIELDS})
        if not _refs_exist(refs, columns):
            warnings.append(
                f"指标「{metric.get('name_zh', metric_id)}」引用的表或列已不存在，未保留"
            )
            continue
        restored = json.loads(json.dumps(metric))
        restored.pop("data_health", None)
        new.setdefault("metrics", {})[metric_id] = restored

    for dim_id, dim in (old.get("dimensions") or {}).items():
        kind = dim.get("kind", "categorical")
        if kind == "derived":
            table = str(dim.get("requires") or "")
            if table not in columns:
                warnings.append(f"派生维度「{dim.get('name_zh', dim_id)}」依赖的表已不存在")
                continue
        else:
            ref = dim.get("column") or dim.get("filter_column")
            if ref and not _refs_exist(_refs(str(ref)), columns):
                warnings.append(f"维度「{dim.get('name_zh', dim_id)}」依赖的列已不存在")
                continue
        new.setdefault("dimensions", {})[dim_id] = json.loads(json.dumps(dim))

    known_dims = set((new.get("dimensions") or {}))
    for metric in (new.get("metrics") or {}).values():
        grains = metric.get("supported_grain") or ["none"]
        metric["supported_grain"] = [
            grain for grain in grains
            if grain in {"none", "month"} or grain in known_dims
        ]
        if not metric["supported_grain"]:
            metric["supported_grain"] = ["none"]

    return new, warnings
