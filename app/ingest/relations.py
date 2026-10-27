"""Relationship inference and a minimal star-schema suggestion.

Only N:1 / 1:1 relationships that do not amplify the child table are accepted.
N:N relationships are reported and rejected; they need a materialized wide
table before they can be analyzed safely.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

import duckdb

from app.ingest.generic_types import TableInfo

MAX_PAIRS = 5000
MIN_CONTAINMENT = 0.98
MIN_SCORE = 0.55


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _normalize_name(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"(_id|id|_key|key|_code|code)$", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)
    return text


def _name_similarity(left: str, right: str) -> float:
    a, b = _normalize_name(left), _normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def _compatible(left: str, right: str) -> bool:
    numeric = {"integer", "decimal"}
    if left in numeric and right in numeric:
        return True
    return left == "varchar" and right == "varchar"


@dataclass
class RelationInfo:
    child: str
    child_col: str
    child_label: str
    parent: str
    parent_col: str
    parent_label: str
    containment: float
    score: float
    child_rows: int = 0
    joined_rows: int = 0
    amplified: bool = False
    accepted: bool = False
    card: str = "N:1"
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelSuggestion:
    facts: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    relations: list[RelationInfo] = field(default_factory=list)

    def accepted(self) -> list[RelationInfo]:
        return [r for r in self.relations if r.accepted]

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "dimensions": self.dimensions,
            "relations": [r.as_dict() for r in self.relations],
        }


def infer_relations(con: duckdb.DuckDBPyConnection,
                    tables: list[TableInfo]) -> ModelSuggestion:
    relations: list[RelationInfo] = []
    parents = [
        t for t in tables
        if t.rows and len(t.grain) == 1
    ]
    pairs = 0
    for child in tables:
        if not child.rows or not child.columns:
            continue
        own_pk = child.grain if len(child.grain) == 1 else []
        candidates = [
            c for c in child.columns
            if c.physical not in set(own_pk)
            and c.inferred_type in {"integer", "decimal", "varchar"}
            and (c.distinct is None or c.distinct <= 20000)
        ]
        for parent in parents:
            if parent.table_id == child.table_id:
                continue
            parent_col = parent.grain[0]
            parent_col_info = parent.column(parent_col)
            if parent_col_info is None:
                continue
            for col in candidates:
                if not _compatible(col.inferred_type, parent_col_info.inferred_type):
                    continue
                pairs += 1
                if pairs > MAX_PAIRS:
                    break
                relation = _score_relation(con, child, col, parent, parent_col_info)
                if relation is not None:
                    relations.append(relation)
            if pairs > MAX_PAIRS:
                break
        if pairs > MAX_PAIRS:
            break

    # Keep the best accepted parent per (child, child_col).
    best: dict[tuple[str, str], RelationInfo] = {}
    for relation in relations:
        key = (relation.child, relation.child_col)
        current = best.get(key)
        if current is None or relation.score > current.score:
            best[key] = relation
    relations = sorted(best.values(), key=lambda r: (r.child, -r.score))

    facts, dimensions = _infer_roles(tables, relations)
    return ModelSuggestion(facts=facts, dimensions=dimensions, relations=relations)


def _score_relation(con: duckdb.DuckDBPyConnection, child: TableInfo,
                    col, parent: TableInfo, parent_col_info) -> RelationInfo | None:
    qchild = _q(child.table_id)
    qparent = _q(parent.table_id)
    qcc, qpc = _q(col.physical), _q(parent_col_info.physical)
    cast_cc = f"CAST({qcc} AS VARCHAR)"
    cast_pc = f"CAST({qpc} AS VARCHAR)"
    child_distinct = con.execute(
        f"SELECT COUNT(DISTINCT {qcc}) FROM {qchild} WHERE {qcc} IS NOT NULL"
    ).fetchone()[0]
    if not child_distinct:
        return None
    overlap = con.execute(
        f"SELECT COUNT(*) FROM "
        f"(SELECT DISTINCT {cast_cc} AS v FROM {qchild} WHERE {qcc} IS NOT NULL) c "
        f"JOIN (SELECT DISTINCT {cast_pc} AS v FROM {qparent} WHERE {qpc} IS NOT NULL) p "
        f"ON c.v = p.v"
    ).fetchone()[0]
    containment = int(overlap) / int(child_distinct)
    if containment < MIN_CONTAINMENT:
        return None

    child_rows, child_nonnull, joined = con.execute(
        f"SELECT COUNT(*), COUNT({qcc}), "
        f"(SELECT COUNT(*) FROM {qchild} c JOIN {qparent} p "
        f"ON CAST(c.{qcc} AS VARCHAR) = CAST(p.{qpc} AS VARCHAR)) "
        f"FROM {qchild}"
    ).fetchone()
    amplified = int(joined) != int(child_nonnull)
    name_sim = _name_similarity(col.label or col.physical,
                                parent_col_info.label or parent_col_info.physical)
    score = 0.45 * containment + 0.20 * 1.0 + 0.20 * name_sim + 0.15 * 1.0
    accepted = score >= MIN_SCORE and not amplified
    reason = ""
    if amplified:
        reason = "连接后行数放大，疑似 N:N 或父键不唯一"
    elif score < MIN_SCORE:
        reason = "名称相似度不足"
    return RelationInfo(
        child=child.table_id,
        child_col=col.physical,
        child_label=col.label,
        parent=parent.table_id,
        parent_col=parent_col_info.physical,
        parent_label=parent_col_info.label,
        containment=round(containment, 4),
        score=round(score, 4),
        child_rows=int(child_rows),
        joined_rows=int(joined),
        amplified=amplified,
        accepted=accepted,
        card="N:1",
        reason=reason,
    )


MEASURE_NAME_RE = re.compile(
    r"金额|价格|价|销售额|收入|成本|利润|运费|数量|件数|销量|评分|分数|"
    r"amount|price|revenue|sales|cost|profit|value|freight|qty|quantity|score",
    re.I,
)
FACT_NAME_RE = re.compile(r"order|transaction|event|fact|item|line|payment|review|"
                          r"订单|流水|明细|记录|交易", re.I)
ID_NAME_RE = re.compile(r"(^|_)(id|key|code|no)$|编号|单号|代码|zip|邮编|phone|电话", re.I)


def _has_measure(table: TableInfo) -> bool:
    for col in table.columns:
        if col.physical in table.grain:
            continue
        if col.inferred_type not in {"integer", "decimal"}:
            continue
        if ID_NAME_RE.search(col.label or "") or ID_NAME_RE.search(col.physical):
            continue
        if MEASURE_NAME_RE.search(col.label or "") or MEASURE_NAME_RE.search(col.physical):
            return True
    return False


def _infer_roles(tables: list[TableInfo],
                 relations: list[RelationInfo]) -> tuple[list[str], list[str]]:
    accepted = [r for r in relations if r.accepted]
    out_degree: dict[str, int] = {}
    in_degree: dict[str, int] = {}
    for relation in accepted:
        out_degree[relation.child] = out_degree.get(relation.child, 0) + 1
        in_degree[relation.parent] = in_degree.get(relation.parent, 0) + 1

    rows = [t.rows for t in tables if t.rows]
    max_rows = max(rows) if rows else 0
    scored: list[tuple[float, TableInfo]] = []
    for table in tables:
        if not table.rows:
            continue
        has_measure = _has_measure(table)
        has_time = any(c.inferred_type in {"date", "datetime"} for c in table.columns)
        out_d = out_degree.get(table.table_id, 0)
        in_d = in_degree.get(table.table_id, 0)
        score = math.log2(table.rows + 1)
        score += 2.2 if has_measure else 0.0
        score += 1.0 if has_time else 0.0
        score += 1.0 * out_d
        if FACT_NAME_RE.search(table.label or ""):
            score += 0.8
        pure_dimension = in_d > 0 and not has_measure and not has_time
        if pure_dimension:
            score -= 3.0
        if max_rows and table.rows < max_rows * 0.15:
            score -= 1.0
        scored.append((score, table))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return [], []
    top = scored[0][0]
    threshold = max(4.0, top * 0.55)
    facts = [t.table_id for score, t in scored[:4] if score >= threshold]
    if not facts:
        facts = [scored[0][1].table_id]
    dimensions = [
        t.table_id for _score, t in scored[1:]
        if t.table_id not in facts and (in_degree.get(t.table_id, 0) > 0 or t.grain)
    ]
    return facts, dimensions
