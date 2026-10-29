"""Per-metric data health, computed once when a dataset is published.

The numbers travel with the semantic layer (metric.data_health) so the query
path can say "this column is 37% empty" or "this metric only covers 2024-05 to
2025-12" without re-scanning the warehouse on every turn.
"""
from __future__ import annotations

import re
from datetime import datetime

import duckdb

from app.domain.semantic.loader import SemanticLayer

COLUMN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z0-9_]+)\b")


def _q(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _measure_ref(metric) -> tuple[str, str] | None:
    for key in ("expr", "numerator", "grain_numerator", "count_orders"):
        text = metric.raw.get(key)
        if not isinstance(text, str):
            continue
        match = COLUMN_RE.search(text)
        if match:
            return match.group(1), match.group(2)
    return None


def _grain_column(sem: SemanticLayer, fact: str) -> str | None:
    """Use the model's own grain, not an order_id convention."""
    grain = (sem.tables.get(fact) or {}).get("grain") or []
    if not grain:
        return None
    return sem.column(f"{fact}.{grain[0]}")


def build_metric_health(sem: SemanticLayer, metric_ids: list[str], db_path,
                        catalog: dict | None = None) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    health: dict[str, dict] = {}
    now = datetime.now().isoformat(timespec="seconds")
    try:
        for metric_id in metric_ids:
            try:
                metric = sem.metric(metric_id)
                fact = metric.fact
                rows = con.execute(f"SELECT COUNT(*) FROM {_q(fact)}").fetchone()[0]
                item: dict = {"rows": int(rows), "generated_at": now}

                measure = _measure_ref(metric)
                if measure is not None and rows:
                    table, column = measure
                    phys = sem.column(f"{table}.{column}")
                    total, non_null = con.execute(
                        f"SELECT COUNT(*), COUNT({_q(phys)}) FROM {_q(table)}"
                    ).fetchone()
                    if total:
                        item["measure_null_rate"] = round(
                            1 - int(non_null) / int(total), 4
                        )

                order_column = _grain_column(sem, fact)
                if order_column:
                    distinct = con.execute(
                        f"SELECT COUNT(DISTINCT {_q(order_column)}) FROM {_q(fact)}"
                    ).fetchone()[0]
                    if distinct:
                        item["distinct_key"] = int(distinct)
                        item["duplicate_key_ratio"] = round(
                            int(rows) / int(distinct), 3
                        )

                time_via = metric.raw.get("time_via")
                if isinstance(time_via, str) and time_via:
                    table, _, _column = time_via.partition(".")
                    phys = sem.column(time_via)
                    lo, hi = con.execute(
                        f"SELECT MIN({_q(phys)}), MAX({_q(phys)}) FROM {_q(table)}"
                    ).fetchone()
                    if lo is not None and hi is not None:
                        item["time_coverage"] = [
                            str(lo)[:10], str(hi)[:10],
                        ]
                health[metric_id] = item
            except Exception:
                continue
    finally:
        con.close()
    return health
