"""Read-only DuckDB execution with timeout, row cap, and JSON-safe results."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from app.config.settings import MAX_ROW_LIMIT, SQL_TIMEOUT_MS, WAREHOUSE_PATH
from app.domain.compiler import Compiled


class QueryTimeout(RuntimeError):
    pass


def warehouse_version(db_path: Path = WAREHOUSE_PATH) -> str:
    """Cheap dataset snapshot fingerprint used by query records."""
    try:
        stat = Path(db_path).stat()
        return f"{stat.st_size}-{int(stat.st_mtime)}"
    except OSError:
        return "unknown"


@dataclass
class ResultSet:
    result_id: str
    columns: list[str]
    column_types: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    sql: str
    params: list
    fingerprint: str
    duration_ms: int
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "result_id": self.result_id,
            "columns": self.columns,
            "column_types": self.column_types,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "sql": self.sql,
            "params": [str(p) for p in self.params],
            "fingerprint": self.fingerprint,
            "duration_ms": self.duration_ms,
            "meta": self.meta,
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return round(float(v), 6)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def execute(compiled: Compiled, db_path: Path = WAREHOUSE_PATH,
            max_rows: int = MAX_ROW_LIMIT, timeout_ms: int = SQL_TIMEOUT_MS) -> ResultSet:
    con = duckdb.connect(str(db_path), read_only=True)
    timer = threading.Timer(timeout_ms / 1000.0, con.interrupt)
    started = time.perf_counter()
    timer.start()
    try:
        cursor = con.execute(compiled.sql, compiled.params)
        rows = cursor.fetchmany(max_rows + 1)
        types = [str(d[1]) for d in cursor.description] if cursor.description else []
    except duckdb.InterruptException as exc:  # type: ignore[attr-defined]
        raise QueryTimeout(f"查询超过 {timeout_ms} ms，请缩小时间或维度范围") from exc
    finally:
        timer.cancel()
        con.close()
    duration = int((time.perf_counter() - started) * 1000)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    columns = list(compiled.columns)
    if len(columns) != len(types):
        columns = [f"col_{i + 1}" for i in range(len(types))]
    return ResultSet(
        result_id="",  # filled by the caller (needs a counter/session)
        columns=columns,
        column_types=types,
        rows=[[_jsonable(v) for v in row] for row in rows],
        row_count=len(rows),
        truncated=truncated,
        sql=compiled.sql,
        params=compiled.params,
        fingerprint=compiled.fingerprint(),
        duration_ms=duration,
        meta={**(compiled.meta or {}), "warehouse_version": warehouse_version(db_path)},
    )
