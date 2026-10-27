"""Query-record verification: re-run a stored plan and compare outcomes.

Reproduction uses the stored QueryPlan, not the natural-language question:
the plan + semantic layer deterministically reproduce SQL and params. Results
are compared by a canonical digest so row order from ties does not matter.
"""
from __future__ import annotations

import hashlib
import json

from app.core.logging import log_event, logger
from app.config.settings import WAREHOUSE_PATH
from app.domain.pipeline import execute_prepared, prepare
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer


def result_digest(columns: list, rows: list) -> str:
    canonical_rows = sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        for row in (rows or [])
    )
    payload = json.dumps(
        {"columns": list(columns or []), "rows": canonical_rows},
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def verify_record(artifact: dict, sem: SemanticLayer,
                  db_path=WAREHOUSE_PATH) -> dict:
    rid = artifact.get("result_id")
    stored_columns = artifact.get("columns") or []
    stored_rows = artifact.get("rows") or []
    stored_meta = artifact.get("meta") or {}
    stored = {
        "fingerprint": artifact.get("fingerprint"),
        "sql": artifact.get("sql"),
        "row_count": artifact.get("row_count"),
        "truncated": bool(artifact.get("truncated")),
        "result_hash": result_digest(stored_columns, stored_rows),
        "warehouse_version": stored_meta.get("warehouse_version"),
    }

    plan_payload = artifact.get("plan")
    if not plan_payload or not artifact.get("sql"):
        return {
            "result_id": rid, "verdict": "unavailable",
            "message": "记录缺少查询计划或 SQL，无法重跑",
            "stored": stored,
        }

    try:
        plan = QueryPlan.model_validate(plan_payload)
        compiled, _ = prepare(plan, sem)
    except Exception as exc:
        log_event(logger, "record.verify.invalid_plan", result_id=rid,
                  error=f"{type(exc).__name__}: {exc}")
        return {
            "result_id": rid, "verdict": "query_changed",
            "message": f"当前语义层已无法编译这条记录：{exc}",
            "stored": stored,
        }

    sql_match = compiled.sql.strip() == str(artifact.get("sql") or "").strip()
    fingerprint_match = compiled.fingerprint(
        compiler_version=int(sem.raw.get("compiler_version", 1)),
        semantic_version=int(sem.version),
    ) == artifact.get("fingerprint")

    try:
        result = execute_prepared(compiled, db_path=db_path)
    except Exception as exc:
        log_event(logger, "record.verify.execution_failed", result_id=rid,
                  error=f"{type(exc).__name__}: {exc}")
        return {
            "result_id": rid, "verdict": "error",
            "message": f"重跑失败：{type(exc).__name__}: {exc}",
            "stored": stored,
            "checks": {"sql_match": sql_match, "fingerprint_match": fingerprint_match},
        }

    current = {
        "fingerprint": compiled.fingerprint(
            compiler_version=int(sem.raw.get("compiler_version", 1)),
            semantic_version=int(sem.version),
        ),
        "sql": compiled.sql,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "columns": result.columns,
        "result_hash": result_digest(result.columns, result.rows),
        "warehouse_version": (result.meta or {}).get("warehouse_version"),
        "duration_ms": result.duration_ms,
    }
    columns_match = list(stored_columns) == list(result.columns)
    rows_match = (
        stored["result_hash"] == current["result_hash"]
        and stored["truncated"] == current["truncated"]
        and stored["row_count"] == current["row_count"]
    )
    warehouse_changed = stored["warehouse_version"] != current["warehouse_version"]

    # SQL is the reproducible artifact. Fingerprints from older records may
    # use different version arguments, so they are reported but do not decide
    # the verdict on their own.
    if not sql_match:
        verdict = "query_changed"
    elif not rows_match:
        verdict = "data_changed"
    else:
        verdict = "reproduced"

    return {
        "result_id": rid,
        "verdict": verdict,
        "stored": stored,
        "current": current,
        "checks": {
            "sql_match": sql_match,
            "fingerprint_match": fingerprint_match,
            "columns_match": columns_match,
            "rows_match": rows_match,
        },
        "warehouse_changed": warehouse_changed,
    }
