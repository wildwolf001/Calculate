"""Materialize an accepted ingest draft into a minimal draft dataset.

This is the last mile between "agent draft" and "loadable dataset": it writes
warehouse.duckdb, binding.yaml, catalog.json, dim_values.json and semantic.yaml.
It intentionally supports the simple single-fact shapes the agent produces today.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from app.ingest.agent.tools.tables import read_table


def _safe_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "decimal"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    return "varchar"


def _translate(expr: str, table_id: str, columns: dict[str, str]) -> str:
    text = str(expr or "")
    for original, logical in sorted(columns.items(), key=lambda item: -len(item[0])):
        text = re.sub(rf"\b{re.escape(original)}\b", f"{table_id}.{logical}", text)
    return text


def _scope(scope: dict[str, Any], table_id: str, columns: dict[str, str]) -> dict[str, Any]:
    return {
        f"{table_id}.{columns[key]}": value
        for key, value in (scope or {}).items()
        if key in columns
    }


def _grain_case(scope: dict[str, Any]) -> str:
    conditions = []
    for ref, value in (scope or {}).items():
        text = str(value).replace("'", "''")
        conditions.append(f"{ref} = '{text}'")
    if not conditions:
        return "MAX(1)"
    return "MAX(CASE WHEN " + " AND ".join(conditions) + " THEN 1 ELSE 0 END)"


def materialize(upload_dir: Path, ingest_dir: Path, root: Path,
                dataset_id: str, name: str) -> dict[str, Any]:
    upload_dir = Path(upload_dir)
    ingest_dir = Path(ingest_dir)
    root = Path(root)
    draft_path = ingest_dir / "semantic_draft.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8")) if draft_path.exists() else {}
    raw_path = ingest_dir / "draft.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {}
    source = draft.get("metrics") or []
    if not source:
        source = [
            {"id": mid, **metric}
            for mid, metric in (raw.get("metrics") or {}).items()
        ]
    source_tables = raw.get("tables") or {}
    if not source_tables:
        tables_payload = draft.get("tables") or {}
        source_tables = tables_payload if isinstance(tables_payload, dict) else {}

    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(root / "warehouse.duckdb"))
    tables: dict[str, Any] = {}
    columns_by_table: dict[str, dict[str, str]] = {}
    catalog_tables: dict[str, Any] = {}
    dim_values: dict[str, list[Any]] = {}
    dimensions: dict[str, Any] = {}
    time_columns: dict[str, str] = {}
    rows_total: dict[str, int] = {}
    name_to_id: dict[str, str] = {}
    try:
        for index, (table_name, info) in enumerate(source_tables.items(), 1):
            file_name = info.get("file") or f"{table_name}.csv"
            path = upload_dir / file_name
            frame = read_table(path)
            table_id = f"t_{index:03d}"
            name_to_id[str(table_name)] = table_id
            columns: dict[str, str] = {}
            types: dict[str, str] = {}
            for col_index, column in enumerate(frame.columns, 1):
                logical = f"c{col_index:02d}"
                columns[str(column)] = logical
                types[logical] = _safe_type(frame[column])
            frame = frame.rename(columns=columns)
            con.register("_frame", frame)
            con.execute(f'CREATE TABLE "{table_id}" AS SELECT * FROM _frame')
            tables[table_id] = {
                "grain": [columns[c] for c in (info.get("grain") or []) if c in columns],
                "time": None,
                "columns": list(columns.values()),
            }
            columns_by_table[table_id] = columns
            rows_total[table_id] = int(len(frame))
            catalog_tables[table_id] = {
                "rows": int(len(frame)),
                "columns": [
                    {"name": logical, "type": types[logical], "label": original}
                    for original, logical in columns.items()
                ],
            }
            for original, logical in columns.items():
                if types[logical] in {"varchar"}:
                    values = [
                        str(v) for v in frame[logical].dropna().unique()[:50]
                    ]
                    if 2 <= len(values) <= 50:
                        dim_id = f"d_{table_id}_{logical}"
                        dimensions[dim_id] = {
                            "id": dim_id,
                            "name_zh": original,
                            "kind": "categorical",
                            "column": f"{table_id}.{logical}",
                            "selected": True,
                            "provenance": "inferred",
                        }
                        dim_values[f"{table_id}.{logical}"] = values
                elif types[logical] == "date":
                    if not tables[table_id].get("time"):
                        tables[table_id]["time"] = logical
                    if not time_columns:
                        time_columns[f"{table_id}.{logical}"] = original
    finally:
        con.close()

    metrics: dict[str, Any] = {}
    components: dict[str, dict[str, Any]] = {}
    for metric in source:
        metric_id = str(metric.get("id"))
        table_name = metric.get("table") or metric.get("fact")
        table_id = name_to_id.get(str(table_name))
        if table_id is None:
            continue
        columns = columns_by_table.get(table_id) or {}
        if metric.get("type") == "ratio":
            continue
        expr = _translate(metric.get("expr") or "", table_id, columns)
        raw = {
            "name_zh": metric.get("name") or metric_id,
            "type": "aggregate",
            "fact": table_id,
            "expr": expr,
            "scope": _scope(metric.get("scope") or {}, table_id, columns),
            "unit": metric.get("unit"),
            "supported_grain": ["none", "month", *dimensions.keys()],
            "provenance": "inferred",
        }
        metrics[metric_id] = raw
        components[metric_id] = raw

    for metric in source:
        if metric.get("type") != "ratio":
            continue
        table_name = metric.get("table") or metric.get("fact")
        table_id = name_to_id.get(str(table_name))
        if table_id is None:
            continue
        columns = columns_by_table.get(table_id) or {}
        numerator = components.get(str(metric.get("numerator")))
        denominator = components.get(str(metric.get("denominator")))
        if not numerator or not denominator:
            continue
        grain = (tables[table_id].get("grain") or [list(columns.values())[:1]])
        key = grain[0]
        metrics[str(metric.get("id"))] = {
            "name_zh": metric.get("name") or metric.get("id"),
            "type": "ratio",
            "fact": table_id,
            "ratio_grain": "fact",
            "ratio_key": f"{table_id}.{key}",
            "numerator": numerator["expr"],
            "denominator": denominator["expr"],
            "grain_numerator": _grain_case(numerator.get("scope") or {}),
            "grain_denominator": _grain_case(denominator.get("scope") or {}),
            "scope": {},
            "time_via": tables[table_id].get("time"),
            "unit": metric.get("unit") or "%",
            "supported_grain": ["none", "month", *dimensions.keys()],
            "provenance": "inferred",
        }

    dimensions["month"] = {
        "id": "month",
        "name_zh": "月份",
        "kind": "time",
        "format": "%Y-%m",
        "selected": True,
        "provenance": "inferred",
    }
    time_range = ["1970-01-01", "2100-01-01"]
    for table_id, info in tables.items():
        if info.get("time"):
            info["time"] = info["time"] if isinstance(info["time"], str) else info["time"]
    semantic = {
        "version": 1,
        "dataset": name,
        "dataset_id": dataset_id,
        "compiler_version": 1,
        "time_range": time_range,
        "default_year": "latest_occurrence",
        "status": "draft",
        "provenance": "inferred",
        "parser": "generic",
        "capabilities": {
            "member_list": True, "ratio": True, "entity_count": True,
            "compare": True, "business_checks": [], "member_list_coverage": None,
        },
        "tables": tables,
        "joins": {},
        "pre_aggregates": {},
        "metrics": metrics,
        "dimensions": dimensions,
        "policies": {},
        "generic": True,
        "documentation": {},
        "time_axes": [],
    }
    (root / "semantic.yaml").write_text(
        yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (root / "binding.yaml").write_text(
        yaml.safe_dump({
            "version": 1,
            "tables": {
                table_id: {logical: logical for logical in info["columns"]}
                for table_id, info in tables.items()
            },
        }, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (root / "catalog.json").write_text(
        json.dumps({"tables": catalog_tables}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / "dim_values.json").write_text(
        json.dumps({"values": dim_values, "aliases": {}, "id_maps": {}},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / "dataset.json").write_text(json.dumps({
        "dataset_id": dataset_id,
        "name": name,
        "status": "draft",
        "engine": "duckdb",
        "semantic": "semantic.yaml",
        "binding": "binding.yaml",
        "warehouse": "warehouse.duckdb",
        "created_at": date.today().isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "dataset_id": dataset_id,
        "name": name,
        "data_version": "sha256:" + hashlib.sha256(
            (root / "warehouse.duckdb").read_bytes()
        ).hexdigest()[:16],
        "semantic_version": 1,
        "generator_version": "ingest-agent-m1",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "dataset_id": dataset_id,
        "root": str(root),
        "tables": {tid: rows_total[tid] for tid in tables},
        "metrics": sorted(metrics),
        "dimensions": len(dimensions),
    }
