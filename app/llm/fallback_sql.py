"""LLM-4: controlled SQL fallback for questions the semantic layer cannot express.

Guards (all mandatory):
- exactly one statement, and it must be a SELECT/WITH;
- every referenced table is in the semantic whitelist;
- no forbidden verbs;
- must contain a time predicate on a known time column;
- LIMIT is injected if absent, capped at MAX_ROW_LIMIT;
- executed on a read-only connection with a timeout.
This path is disabled by default (`FALLBACK_SQL_ENABLED=false`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

import sqlglot
from sqlglot import exp

from app.config.settings import MAX_ROW_LIMIT
from app.domain.executor import ResultSet, QueryTimeout
from app.domain.semantic.loader import SemanticLayer
from app.llm.client import LLMClient, LLMError, LLMUnavailable
from app.llm.schema_card import build_schema_card

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|"
    r"COPY|EXPORT|IMPORT|PRAGMA|CALL|INSTALL|LOAD|VACUUM|CHECKPOINT)\b",
    re.IGNORECASE,
)


class FallbackSQLError(RuntimeError):
    pass


def _time_columns(sem: SemanticLayer) -> set[str]:
    cols = {"purchase_at", "delivered_at", "review_created_at", "shipping_limit_at"}
    for table in sem.tables.values():
        if table.get("time"):
            cols.add(table["time"])
    return cols


def validate_sql(sql: str, sem: SemanticLayer) -> str:
    if FORBIDDEN.search(sql):
        raise FallbackSQLError("SQL 含禁止关键字")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as exc:
        raise FallbackSQLError(f"SQL 解析失败: {exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise FallbackSQLError("只允许单条 SQL")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise FallbackSQLError("只允许 SELECT/UNION")

    tables = {t.name for t in stmt.find_all(exp.Table)} - {""}
    unknown = tables - set(sem.tables)
    if unknown:
        raise FallbackSQLError(f"SQL 引用了白名单之外的表: {sorted(unknown)}")

    time_cols = _time_columns(sem)
    searched = {c.name for c in stmt.find_all(exp.Column)}
    if not (searched & time_cols):
        raise FallbackSQLError("SQL 缺少时间过滤（必须引用时间列）")

    if stmt.args.get("limit") is None:
        sql = sql.rstrip().rstrip(";") + f" LIMIT {MAX_ROW_LIMIT}"
    else:
        try:
            limit = int(stmt.args["limit"].expression.name)
        except Exception:
            raise FallbackSQLError("LIMIT 必须是整数")
        if limit > MAX_ROW_LIMIT:
            raise FallbackSQLError(f"LIMIT 超过上限 {MAX_ROW_LIMIT}")
    return sql


def generate_sql(question: str, sem: SemanticLayer, client: LLMClient | None = None,
                 error: str | None = None) -> str:
    client = client or LLMClient()
    if not client.available:
        raise LLMUnavailable("未配置 LLM，无法使用 SQL 兜底")
    system = Template((PROMPT_DIR / "fallback_sql_system.txt").read_text(encoding="utf-8")).substitute(
        schema_card=build_schema_card(sem), max_rows=MAX_ROW_LIMIT,
    )
    user = json.dumps({"question": question, "previous_error": error}, ensure_ascii=False)
    text = client.complete_text(system, user, max_tokens=1400, stage="fallback_sql")
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S)
    sql = (match.group(1) if match else text).strip()
    return validate_sql(sql, sem)


def execute_fallback(sql: str) -> ResultSet:
    import threading
    import time as _time

    import duckdb

    from app.config.settings import SQL_TIMEOUT_MS, WAREHOUSE_PATH

    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    timer = threading.Timer(SQL_TIMEOUT_MS / 1000.0, con.interrupt)
    started = _time.perf_counter()
    timer.start()
    try:
        cursor = con.execute(sql)
        rows = cursor.fetchmany(MAX_ROW_LIMIT + 1)
        types = [str(d[1]) for d in cursor.description] if cursor.description else []
        columns = [d[0] for d in cursor.description] if cursor.description else []
    except duckdb.InterruptException as exc:  # type: ignore[attr-defined]
        raise QueryTimeout("兜底 SQL 超时") from exc
    finally:
        timer.cancel()
        con.close()
    truncated = len(rows) > MAX_ROW_LIMIT
    rows = rows[:MAX_ROW_LIMIT]
    return ResultSet(
        result_id="", columns=columns, column_types=types,
        rows=[[v.isoformat() if hasattr(v, "isoformat") else v for v in row] for row in rows],
        row_count=len(rows), truncated=truncated, sql=sql, params=[], fingerprint="fallback",
        duration_ms=int((_time.perf_counter() - started) * 1000), meta={"fallback_sql": True},
    )
