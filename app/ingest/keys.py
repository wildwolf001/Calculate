"""Primary-key / grain detection for arbitrary tables.

A unique amount column is not a primary key. Measure-like columns are
penalized, and composite key candidates are tried before accepting a
measure-like single column.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

import duckdb

KEY_HINT_RE = re.compile(r"(^|_)(id|key|code|no)$|编号|单号|代码|主键|序号", re.I)
SEQUENCE_HINT_RE = re.compile(r"sequence|序号|item_id|line_no|^no$|^seq", re.I)
MEASURE_HINT_RE = re.compile(
    r"金额|价格|价|销售额|收入|成本|利润|运费|数量|件数|销量|评分|分数|"
    r"amount|price|revenue|sales|cost|profit|value|freight|qty|quantity|"
    r"score|length|weight|height|width",
    re.I,
)


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass
class GrainResult:
    grain: list[str]
    duplicate_rows: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


def _name_score(label: str, physical: str) -> int:
    if KEY_HINT_RE.search(label or "") or KEY_HINT_RE.search(physical):
        return 3
    if (label or "").lower().endswith("id"):
        return 3
    return 0


def _measure_like(label: str, physical: str) -> bool:
    return bool(MEASURE_HINT_RE.search(label or "") or MEASURE_HINT_RE.search(physical))


def _rank(stat: dict[str, Any]) -> tuple[int, float]:
    score = _name_score(stat["label"], stat["physical"])
    if SEQUENCE_HINT_RE.search(stat["label"] or "") or SEQUENCE_HINT_RE.search(stat["physical"]):
        score += 2
    if _measure_like(stat["label"], stat["physical"]):
        score -= 4
    if stat["type"] in {"decimal"}:
        score -= 1
    return (-score, -(stat["distinct"] / max(1, stat["_rows"])))


def _composite(con: duckdb.DuckDBPyConnection, table: str, rows: int,
               pool: list[dict[str, Any]]) -> list[str] | None:
    qt = _q(table)
    for size in (2, 3):
        for combo in itertools.combinations(pool[:6], size):
            expr = " || '|' || ".join(
                f"CAST({_q(s['physical'])} AS VARCHAR)" for s in combo
            )
            distinct = con.execute(
                f"SELECT COUNT(DISTINCT {expr}) FROM {qt}"
            ).fetchone()[0]
            if int(distinct) == int(rows):
                return [s["physical"] for s in combo]
    return None


def detect_grain(con: duckdb.DuckDBPyConnection, table: str,
                 columns: list[tuple[str, str, str]]) -> GrainResult:
    """columns: [(physical, label, inferred_type)]"""
    qt = _q(table)
    rows = con.execute(f"SELECT COUNT(*) FROM {qt}").fetchone()[0]
    if rows == 0 or not columns:
        return GrainResult([], 0, [])

    stats: list[dict[str, Any]] = []
    for physical, label, inferred_type in columns:
        qc = _q(physical)
        nulls, distinct = con.execute(
            f"SELECT COUNT(*) - COUNT({qc}), COUNT(DISTINCT {qc}) FROM {qt}"
        ).fetchone()
        stats.append({
            "physical": physical,
            "label": label,
            "type": inferred_type,
            "nulls": int(nulls),
            "distinct": int(distinct),
            "unique": int(nulls) == 0 and int(distinct) == int(rows),
            "_rows": int(rows),
        })

    singles = [s for s in stats if s["unique"]]
    singles.sort(key=_rank)
    if singles and not _measure_like(singles[0]["label"], singles[0]["physical"]):
        return GrainResult(
            grain=[singles[0]["physical"]],
            duplicate_rows=0,
            candidates=[_evidence(s, "single") for s in singles[:5]],
        )

    # Either no unique single, or the only unique columns look like measures.
    pool = [s for s in stats if s["nulls"] == 0]
    pool.sort(key=_rank)
    composite = _composite(con, table, int(rows), pool)
    if composite:
        return GrainResult(
            grain=composite,
            duplicate_rows=0,
            candidates=[
                _evidence(s, "composite") for s in pool if s["physical"] in composite
            ],
            note="单列唯一项像度量，已优先采用组合键",
        )

    if singles:
        return GrainResult(
            grain=[],
            duplicate_rows=0,
            candidates=[_evidence(s, "measure-like-unique") for s in singles[:5]],
            note="唯一列像金额/度量，未作为主键；请人工确认粒度",
        )

    duplicate_rows = int(rows)
    if columns:
        expr = " || '|' || ".join(
            f"CAST({_q(c[0])} AS VARCHAR)" for c in columns
        )
        distinct_rows = con.execute(
            f"SELECT COUNT(DISTINCT {expr}) FROM {qt}"
        ).fetchone()[0]
        duplicate_rows = int(rows) - int(distinct_rows)
    return GrainResult(
        grain=[],
        duplicate_rows=duplicate_rows,
        candidates=[_evidence(s, "no-unique-key") for s in pool[:5]],
        note="没有可信的唯一键",
    )


def _evidence(stat: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "column": stat["physical"],
        "label": stat["label"],
        "type": stat["type"],
        "nulls": stat["nulls"],
        "distinct": stat["distinct"],
        "kind": kind,
        "measure_like": _measure_like(stat["label"], stat["physical"]),
    }
