"""Generic physical catalog and human-readable data profile."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb

from app.ingest.generic_types import TableInfo, jsonable


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _date_only(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def build_catalog(con: duckdb.DuckDBPyConnection,
                  tables: list[TableInfo],
                  show_pii_samples: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"tables": {}}
    for table in tables:
        if table.rows == 0:
            continue
        qt = _q(table.table_id)
        columns: list[dict[str, Any]] = []
        for col in table.columns:
            qc = _q(col.physical)
            nulls, distinct = con.execute(
                f"SELECT COUNT(*) - COUNT({qc}), COUNT(DISTINCT {qc}) FROM {qt}"
            ).fetchone()
            info: dict[str, Any] = {
                "name": col.physical,
                "label": col.label,
                "type": col.inferred_type,
                "nulls": int(nulls),
                "null_rate": round(int(nulls) / table.rows, 4) if table.rows else None,
                "distinct": int(distinct),
                "unique": int(nulls) == 0 and int(distinct) == int(table.rows),
                "is_pii": col.is_pii,
            }
            if (show_pii_samples or not col.is_pii) and int(distinct) <= 60:
                rows = con.execute(
                    f"SELECT DISTINCT {qc} FROM {qt} WHERE {qc} IS NOT NULL "
                    f"ORDER BY 1 LIMIT 20"
                ).fetchall()
                info["samples"] = [jsonable(r[0]) for r in rows]
            columns.append(info)
        out["tables"][table.table_id] = {"rows": table.rows, "columns": columns}
    return out


def build_profile(dataset_name: str, tables: list[TableInfo],
                  catalog: dict[str, Any], quality: dict[str, Any],
                  options: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog_tables = catalog.get("tables", {})
    total_rows = sum(t.get("rows", 0) for t in catalog_tables.values())
    total_columns = sum(len(t.get("columns", [])) for t in catalog_tables.values())
    time_values: list[str] = []
    table_profiles: list[dict[str, Any]] = []
    for table in tables:
        if table.rows == 0:
            continue
        cat_table = catalog_tables.get(table.table_id, {})
        columns: list[dict[str, Any]] = []
        for col in table.columns:
            is_time = col.inferred_type in {"date", "datetime"}
            entry = {
                "physical": col.physical,
                "label": col.label,
                "type": col.inferred_type,
                "unit": col.unit,
                "null_rate": col.null_rate,
                "distinct": col.distinct,
                "min": _date_only(col.minimum) if is_time else jsonable(col.minimum),
                "max": _date_only(col.maximum) if is_time else jsonable(col.maximum),
                "top_values": [
                    {"value": jsonable(v.get("value")), "count": v.get("count")}
                    for v in col.top_values
                ],
                "pii": col.is_pii,
                "warnings": col.warnings,
            }
            columns.append(entry)
            if col.inferred_type in {"date", "datetime"} and col.minimum and col.maximum:
                time_values.extend([_date_only(col.minimum), _date_only(col.maximum)])
        table_profiles.append({
            "table_id": table.table_id,
            "label": table.label,
            "rows": table.rows,
            "grain": table.grain,
            "duplicate_rows": table.duplicate_rows,
            "header_row": table.header_row,
            "warnings": table.warnings,
            "columns": columns,
        })
    time_range = [min(time_values), max(time_values)] if time_values else None
    profile = {
        "dataset": dataset_name,
        "overview": {
            "tables": len(table_profiles),
            "rows": total_rows,
            "columns": total_columns,
            "time_range": time_range,
            "entities": {t["table_id"]: t["rows"] for t in table_profiles},
        },
        "tables": table_profiles,
        "quality": quality.get("checks", []),
        "warnings": quality.get("warnings", []),
        "options": options or {},
    }
    profile["summary"] = summarize(profile)
    return profile


def summarize(profile: dict[str, Any]) -> str:
    overview = profile.get("overview", {})
    tables = profile.get("tables", [])
    parts: list[str] = []
    parts.append(
        f"数据源包含 {overview.get('tables', 0)} 张表、{overview.get('rows', 0):,} 行、"
        f"{overview.get('columns', 0)} 列。"
    )
    table_bits = []
    for table in tables[:6]:
        grain = "、".join(table.get("grain") or []) or "无唯一主键"
        table_bits.append(f"{table.get('label')}({table.get('rows'):,} 行，粒度 {grain})")
    if table_bits:
        parts.append("表：" + "；".join(table_bits) + "。")
    if overview.get("time_range"):
        parts.append(f"数据时间范围 {overview['time_range'][0]} 至 {overview['time_range'][1]}。")
    issues = [
        q for q in profile.get("quality", [])
        if q.get("severity") in {"error", "warn"} and (q.get("count") or 0) > 0
    ]
    if issues:
        parts.append("需要关注：" + "；".join(
            f"{q.get('title')}（{q.get('count')}）" for q in issues[:5]
        ) + "。")
    else:
        parts.append("未发现阻断级质量问题。")
    warnings = profile.get("warnings") or []
    if warnings:
        parts.append("解析提示：" + "；".join(warnings[:5]) + "。")
    return "".join(parts)
