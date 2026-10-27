"""Quality checks that do not depend on any business meaning."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from app.ingest.generic_types import TableInfo


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def run_generic_quality(con: duckdb.DuckDBPyConnection,
                        tables: list[TableInfo],
                        catalog: dict[str, Any],
                        pii_policy: str = "mask",
                        totals_policy: str = "exclude") -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    no_key = [t.table_id for t in tables if t.rows and not t.grain]
    checks.append({
        "id": "no_unique_key",
        "severity": "warn",
        "title": "部分表没有唯一主键",
        "count": len(no_key),
        "detail": no_key,
    })

    duplicates = {
        t.table_id: t.duplicate_rows for t in tables if t.duplicate_rows
    }
    checks.append({
        "id": "duplicate_full_rows",
        "severity": "warn",
        "title": "存在重复整行",
        "count": sum(duplicates.values()),
        "detail": duplicates,
    })

    empty_cols: dict[str, list[str]] = {}
    missing_values: dict[str, dict[str, int]] = {}
    constant_cols: dict[str, list[str]] = {}
    pii_cols: dict[str, list[str]] = {}
    weak_cols: dict[str, list[str]] = {}
    negative: dict[str, int] = {}
    totals: dict[str, int] = {}

    for table in tables:
        if not table.rows:
            continue
        empty_cols[table.table_id] = [
            c.physical for c in table.columns
            if c.null_rate >= 0.999
        ]
        missing_values[table.table_id] = {
            c.physical: int(c.null_count)
            for c in table.columns
            if 0 < int(c.null_count) < table.rows
        }
        constant_cols[table.table_id] = [
            c.physical for c in table.columns
            if c.distinct == 1
        ]
        pii_cols[table.table_id] = [c.physical for c in table.columns if c.is_pii]
        weak_cols[table.table_id] = [
            c.physical for c in table.columns
            if c.confidence < 0.98 or c.warnings
        ]
        totals[table.table_id] = len(table.totals_rows)
        for col in table.columns:
            if col.inferred_type not in {"integer", "decimal"}:
                continue
            qc = _q(col.physical)
            count = con.execute(
                f"SELECT COUNT(*) FROM {_q(table.table_id)} WHERE {qc} < 0"
            ).fetchone()[0]
            if count:
                negative[f"{table.table_id}.{col.physical}"] = int(count)

    checks.extend([
        {
            "id": "empty_columns",
            "severity": "warn",
            "title": "存在全空列",
            "count": sum(len(v) for v in empty_cols.values()),
            "detail": {k: v for k, v in empty_cols.items() if v},
        },
        {
            "id": "missing_values",
            "severity": "warn",
            "title": "部分字段存在缺失值，聚合结果按可用记录计算",
            "count": sum(
                sum(values.values()) for values in missing_values.values()
            ),
            "detail": {
                table_id: values
                for table_id, values in missing_values.items()
                if values
            },
        },
        {
            "id": "constant_columns",
            "severity": "info",
            "title": "存在常量列",
            "count": sum(len(v) for v in constant_cols.values()),
            "detail": {k: v for k, v in constant_cols.items() if v},
        },
        {
            "id": "negative_values",
            "severity": "warn",
            "title": "数值列存在负值（可能是退款/修正，也可能出错）",
            "count": sum(negative.values()),
            "detail": negative,
        },
        {
            "id": "pii_columns",
            "severity": "info",
            "title": (
                "疑似个人信息列，样例已按配置脱敏（数据本身未删除）"
                if pii_policy == "mask"
                else "疑似个人信息列，样例按配置显示（数据未被删除）"
            ),
            "count": sum(len(v) for v in pii_cols.values()),
            "detail": {k: v for k, v in pii_cols.items() if v},
        },
        {
            "id": "low_confidence_columns",
            "severity": "info",
            "title": "类型推断置信度不足或存在转换告警",
            "count": sum(len(v) for v in weak_cols.values()),
            "detail": {k: v for k, v in weak_cols.items() if v},
        },
        {
            "id": "totals_rows",
            "severity": "info" if totals_policy == "exclude" else "warn",
            "title": (
                "检测到合计/小计行（已从分析表排除，避免重复计数）"
                if totals_policy == "exclude"
                else "检测到合计/小计行（按你的选择保留，可能导致重复计数）"
            ),
            "count": sum(totals.values()),
            "detail": {k: v for k, v in totals.items() if v},
        },
    ])

    for table in tables:
        warnings.extend(f"{table.label}: {w}" for w in table.warnings)
    if not warnings:
        warnings = []
    return {
        "checks": checks,
        "warnings": warnings,
        "policy": {"pii": pii_policy, "totals": totals_policy},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
