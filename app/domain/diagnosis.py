"""Explain why a query returned zero rows, using cheap deterministic probes.

The answer is a diagnosis code plus facts, so the composer can say *why* there
is no data instead of a generic "没有可用数据".
"""
from __future__ import annotations

import re
from typing import Any

import duckdb

from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer

COLUMN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z0-9_]+)\b")


def _q(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params: tuple = ()) -> int | None:
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _measure_ref(metric: Any) -> tuple[str, str] | None:
    for key in ("expr", "numerator", "grain_numerator", "count_orders"):
        text = metric.raw.get(key)
        if not isinstance(text, str):
            continue
        match = COLUMN_RE.search(text)
        if match:
            return match.group(1), match.group(2)
    return None


def diagnose_empty(plan: QueryPlan, sem: SemanticLayer,
                   db_path) -> dict[str, Any] | None:
    if plan.kind != "aggregate" or not plan.metric:
        return None
    try:
        if plan.metric_spec:
            from app.domain.semantic.loader import Metric

            metric = Metric(plan.metric, plan.metric_spec)
        else:
            metric = sem.metric(plan.metric)
    except Exception:
        return None

    fact = metric.fact
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        facts: dict[str, Any] = {"fact": fact}
        total = _scalar(con, f"SELECT COUNT(*) FROM {_q(fact)}")
        facts["fact_rows"] = total
        if total == 0:
            return _result("FACT_EMPTY", facts,
                           f"指标「{metric.name}」对应的数据表没有任何记录。")

        time_via = metric.raw.get("time_via")
        if plan.time is not None and time_via:
            phys = sem.column(str(time_via))
            in_window = _scalar(
                con,
                f"SELECT COUNT(*) FROM {_q(fact)} "
                f"WHERE {_q(fact)}.{_q(phys)} >= ? AND {_q(fact)}.{_q(phys)} < ?",
                (str(plan.time.start), str(plan.time.end)),
            )
            facts["rows_in_window"] = in_window
            if in_window == 0:
                return _result(
                    "NO_ROWS_IN_WINDOW", facts,
                    f"指标「{metric.name}」在 {plan.time.start} ~ {plan.time.end} "
                    f"内没有任何记录（按 {time_via} 统计）。",
                )

        local_filters = []
        for dim_id, spec in (plan.filters or {}).items():
            dim = sem.dimensions.get(dim_id)
            if dim is None:
                continue
            ref = dim.raw.get("filter_column") or dim.raw.get("column")
            if not ref or str(ref).split(".", 1)[0] != fact:
                continue
            local_filters.append((dim_id, spec, str(ref)))
        if local_filters:
            clauses, params = [], []
            for _dim_id, spec, ref in local_filters:
                phys = sem.column(ref)
                raw = spec.value
                values = list(raw) if isinstance(raw, (list, tuple, set)) else [raw]
                if not values:
                    continue
                marks = ", ".join("?" for _ in values)
                clauses.append(f"{_q(fact)}.{_q(phys)} IN ({marks})")
                params.extend(values)
            if clauses:
                rows = _scalar(
                    con,
                    f"SELECT COUNT(*) FROM {_q(fact)} WHERE " + " AND ".join(clauses),
                    tuple(params),
                )
                facts["rows_with_filters"] = rows
                if rows == 0:
                    labels = "、".join(
                        sem.dimensions[dim_id].raw.get("name_zh", dim_id)
                        for dim_id, _spec, _ref in local_filters
                    )
                    return _result(
                        "FILTER_EXCLUDES_ALL", facts,
                        f"筛选条件（{labels}）在数据里没有任何匹配记录。",
                    )

        measure = _measure_ref(metric)
        if measure is not None:
            table, column = measure
            phys = sem.column(f"{table}.{column}")
            non_null = _scalar(
                con, f"SELECT COUNT({_q(phys)}) FROM {_q(table)}"
            )
            facts["measure_non_null"] = non_null
            if non_null == 0:
                return _result(
                    "MEASURE_ALL_NULL", facts,
                    f"指标「{metric.name}」依赖的列 {table}.{column} 全部为空。",
                )

        return _result(
            "NO_MATCHING_ROWS", facts,
            "时间范围、筛选条件和指标口径的组合没有匹配记录。",
        )
    finally:
        con.close()


def _result(code: str, facts: dict[str, Any], message: str) -> dict[str, Any]:
    return {"code": code, "facts": facts, "message": message}
