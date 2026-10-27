"""Generate a reviewable semantic layer draft from inferred structure.

Everything produced here is marked `provenance: auto` and `status: draft`.
Business scope (e.g. "only delivered orders") is never inferred.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.ingest.generic_types import TableInfo, jsonable
from app.ingest.relations import ModelSuggestion, RelationInfo

MAX_DIM_CARDINALITY = 50
MAX_LABEL_CARDINALITY = 20000
MEASURE_HINTS = re.compile(
    r"金额|价格|价|销售额|收入|成本|利润|运费|数量|件数|销量|"
    r"amount|price|revenue|sales|cost|profit|value|freight|qty|quantity",
    re.I,
)
SCORE_HINTS = re.compile(r"评分|分数|score", re.I)
RATE_HINTS = re.compile(r"率|比例|占比|rate|ratio|percent|avg|平均", re.I)
COUNT_HINTS = re.compile(r"数量|件数|次数|销量|qty|quantity|count", re.I)
CATEGORICAL_HINTS = re.compile(
    r"status|type|city|state|category|installments|sequence|level|"
    r"状态|类型|城市|省份|品类|类目|分期|等级",
    re.I,
)
COUNT_ID_RE = re.compile(r"(^|_)id$|编号", re.I)
NON_COUNT_ID = re.compile(
    r"zip|邮编|phone|电话|tel|code|编码|序号|sequence|item_id|line_id",
    re.I,
)
LABEL_PREF = re.compile(
    r"name|title|label|city|state|category|名称|标题|城市|省份|品类|类目",
    re.I,
)
TEXT_DIM_EXCLUDE = re.compile(
    r"message|content|description|备注|说明|文本|正文|地址|address",
    re.I,
)
SYNONYM_RULES = [
    (re.compile(r"^price$|^price_", re.I),
     ["销售额", "商品销售额", "销售金额", "GMV", "营收"]),
    (re.compile(r"base_price", re.I), ["标价", "商品标价"]),
    (re.compile(r"freight", re.I), ["运费", "物流费"]),
    (re.compile(r"payment_value", re.I), ["支付金额", "实付金额"]),
    (re.compile(r"review_score|score", re.I), ["评分", "评价分数"]),
    (re.compile(r"(^|_)product_id$", re.I),
     ["商品数", "商品种类", "几种商品", "商品款数"]),
    (re.compile(r"(^|_)order_id$", re.I), ["订单数", "订单量", "订单笔数"]),
    (re.compile(r"customer.*id", re.I), ["客户数", "买家数", "客户人数"]),
    (re.compile(r"(^|_)seller_id$", re.I), ["商家数", "卖家数"]),
    (re.compile(r"(^|_)review_id$", re.I), ["评价数", "评论数"]),
    (re.compile(r"category_id", re.I), ["品类数", "类目数"]),
]
ID_LIKE = re.compile(
    r"(^|_)(id|key|code|no)$|编号|单号|代码|邮编|zip|postal|phone|tel|手机|电话",
    re.I,
)


def _slug(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value)).strip("_")
    return text or "x"


def _suffix(table_id: str) -> str:
    return table_id.replace("t_", "")


def _short_label(label: str | None) -> str:
    """Drop the '<file>/' prefix added by multi-sheet workbooks."""
    text = str(label or "").strip()
    return text.rsplit("/", 1)[-1] if "/" in text else text


def _qlabel(label: str) -> str:
    return (label or "").strip() or "未命名列"


DIM_SYNONYMS = [
    (re.compile(r"customer_city", re.I), ["客户城市", "城市", "地区"]),
    (re.compile(r"customer_state", re.I), ["客户省份", "省份", "州"]),
    (re.compile(r"order_status", re.I), ["订单状态", "状态"]),
    (re.compile(r"product_name", re.I), ["商品", "商品名称", "产品"]),
    (re.compile(r"seller_name", re.I), ["商家", "商家名称", "卖家"]),
    (re.compile(r"category_name_zh|category_name_en", re.I), ["品类", "类目", "分类"]),
    (re.compile(r"^category$", re.I), ["品类", "类目", "分类"]),
    (re.compile(r"payment_type", re.I), ["支付方式", "付款方式"]),
    (re.compile(r"review_score", re.I), ["评分", "分数"]),
    (re.compile(r"(^|_)city$", re.I), ["城市"]),
    (re.compile(r"(^|_)state$", re.I), ["省份", "州"]),
    (re.compile(r"installments", re.I), ["分期期数", "分期"]),
]


DIM_DISPLAY = [
    (re.compile(r"customer_city", re.I), "客户城市"),
    (re.compile(r"customer_state", re.I), "客户省份"),
    (re.compile(r"order_status", re.I), "订单状态"),
    (re.compile(r"review_score", re.I), "评分"),
    (re.compile(r"seller_state", re.I), "商家省份"),
    (re.compile(r"seller_city", re.I), "商家城市"),
    (re.compile(r"product_name", re.I), "商品名称"),
    (re.compile(r"seller_name", re.I), "商家名称"),
    (re.compile(r"category_name_zh|category_name_en", re.I), "品类"),
    (re.compile(r"^category$", re.I), "品类"),
    (re.compile(r"payment_type", re.I), "支付方式"),
    (re.compile(r"installments", re.I), "分期期数"),
    (re.compile(r"(^|_)city$", re.I), "城市"),
    (re.compile(r"(^|_)state$", re.I), "省份"),
]


def _dim_name(label: str, table_label: str | None = None) -> str:
    for pattern, name in DIM_DISPLAY:
        if pattern.search(label or ""):
            return name
    return _qlabel(label)


def _dim_synonyms(label: str, table_label: str | None = None) -> list[str]:
    out = [_qlabel(label)]
    for pattern, aliases in DIM_SYNONYMS:
        if pattern.search(label or ""):
            out.extend(aliases)
    if table_label:
        out.append(_short_label(table_label))
    return list(dict.fromkeys(x for x in out if x))


def _label_column(table: TableInfo, parent_pk: str) -> str | None:
    labels = {c.label for c in table.columns}
    candidates = []
    for col in table.columns:
        if col.physical == parent_pk:
            continue
        if (col.label or "").endswith("_name_en") and (
            (col.label or "").replace("_name_en", "_name_zh") in labels
        ):
            continue
        if col.inferred_type not in {"varchar", "boolean"}:
            continue
        if col.distinct is None or col.distinct > MAX_LABEL_CARDINALITY:
            continue
        score = 0.0
        if LABEL_PREF.search(col.label or ""):
            score += 3.0
        if col.is_pii:
            score -= 3.0
        if ID_LIKE.search(col.label or "") or ID_LIKE.search(col.physical):
            score -= 4.0
        if (col.distinct or 0) > 1:
            score += 1.0
        candidates.append((score, col))
    if not candidates:
        return parent_pk
    candidates.sort(key=lambda item: (item[0], item[1].distinct or 0), reverse=True)
    return candidates[0][1].physical


def _fact_dimensions(fact: TableInfo,
                     relations: list[RelationInfo],
                     table_map: dict[str, TableInfo],
                     selected: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (dimension defs, dimension candidates) available to one fact."""
    dimensions: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    suffix = _suffix(fact.table_id)

    for col in fact.columns:
        if col.is_pii or col.physical in set(fact.grain):
            continue
        if ID_LIKE.search(col.label or "") or ID_LIKE.search(col.physical):
            continue
        if TEXT_DIM_EXCLUDE.search(col.label or ""):
            continue
        if col.distinct is None or col.distinct < 2 or col.distinct > MAX_DIM_CARDINALITY:
            continue
        if col.inferred_type in {"varchar", "boolean"}:
            is_categorical = True
        elif col.inferred_type in {"integer", "decimal"}:
            is_categorical = bool(
                CATEGORICAL_HINTS.search(col.label or "")
                or CATEGORICAL_HINTS.search(col.physical)
            ) and col.distinct <= 20
        else:
            continue
        if not is_categorical:
            continue
        if col.inferred_type in {"integer", "decimal"} and (
            MEASURE_HINTS.search(col.label or "") or SCORE_HINTS.search(col.label or "")
        ):
            continue
        dim_id = f"d_{suffix}_{col.physical}".lower()
        dimensions.append({
            "id": dim_id,
            "name_zh": _dim_name(col.label, fact.label),
            "column": f"{fact.table_id}.{col.physical}",
            "filter_column": f"{fact.table_id}.{col.physical}",
            "filter_lookup": f"{fact.table_id}.{col.physical}",
            "value_source": fact.table_id,
            "synonyms": _dim_synonyms(col.label, fact.label),
            "selected": bool(selected),
            "provenance": "auto",
            "confidence": round(col.confidence * 0.95, 4),
        })
        candidates.append({
            "id": dim_id,
            "name_zh": _dim_name(col.label, fact.label),
            "kind": "categorical",
            "table": fact.table_id,
            "column": col.physical,
            "cardinality": col.distinct,
            "synonyms": _dim_synonyms(col.label, fact.label),
            "selected": bool(selected),
            "confidence": round(col.confidence * 0.95, 4),
        })

    for relation in relations:
        if relation.child != fact.table_id or not relation.accepted:
            continue
        parent = table_map.get(relation.parent)
        if parent is None:
            continue
        label_cols = _label_columns(parent, relation.parent_col, limit=2)
        if not label_cols:
            fallback = parent.column(relation.parent_col)
            label_cols = [fallback] if fallback else []
        for label_info in label_cols:
            label_col = label_info.physical
            label = label_info.label
            dim_id = f"d_{suffix}_{_suffix(parent.table_id)}_{label_col}".lower()
            if any(d["id"] == dim_id for d in dimensions):
                continue
            dimensions.append({
                "id": dim_id,
                "name_zh": _dim_name(label, parent.label),
                "column": f"{parent.table_id}.{label_col}",
                # Auto-drafted labels are what users see and type; filtering on
                # the parent key would compare a display value with an id.
                "filter_column": f"{parent.table_id}.{label_col}",
                "filter_lookup": f"{parent.table_id}.{label_col}",
                "value_source": parent.table_id,
                "join_path": [f"{fact.table_id} -> {parent.table_id}"],
                "synonyms": _dim_synonyms(label, parent.label),
                "selected": bool(selected),
                "provenance": "auto",
                "confidence": round(relation.score * 0.9, 4),
            })
            candidates.append({
                "id": dim_id,
                "name_zh": _dim_name(label, parent.label),
                "kind": "categorical",
                "table": parent.table_id,
                "column": label_col,
                "join_path": [f"{fact.table_id} -> {parent.table_id}"],
                "synonyms": _dim_synonyms(label, parent.label),
                "selected": bool(selected),
                "confidence": round(relation.score * 0.9, 4),
            })

    return dimensions, candidates


def _label_columns(table: TableInfo, pk: str, limit: int = 3) -> list:
    scored = []
    labels = {c.label for c in table.columns}
    for col in table.columns:
        if col.physical == pk or col.is_pii:
            continue
        if (col.label or "").endswith("_name_en") and (
            (col.label or "").replace("_name_en", "_name_zh") in labels
        ):
            continue
        if col.inferred_type not in {"varchar", "boolean"}:
            continue
        if col.distinct is None or col.distinct < 2 or col.distinct > MAX_LABEL_CARDINALITY:
            continue
        if ID_LIKE.search(col.label or "") or ID_LIKE.search(col.physical):
            continue
        if TEXT_DIM_EXCLUDE.search(col.label or ""):
            continue
        score = 3.0 if LABEL_PREF.search(col.label or "") else 0.0
        score += 1.0 if (col.distinct or 0) > 1 else 0.0
        scored.append((score, col.distinct or 0, col))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def _transitive_dimensions(fact: TableInfo,
                           relations: list[RelationInfo],
                           table_map: dict[str, TableInfo],
                           selected: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    direct = [
        r for r in relations
        if r.accepted and r.child == fact.table_id
    ]
    direct_parents = {r.parent for r in direct}
    dimensions: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_parents: set[str] = set()

    for first in direct:
        for second in relations:
            if not second.accepted or second.child != first.parent:
                continue
            grand = table_map.get(second.parent)
            if grand is None or grand.table_id == fact.table_id:
                continue
            if grand.table_id in direct_parents or grand.table_id in seen_parents:
                continue
            columns = _label_columns(grand, second.parent_col)
            if not columns:
                continue
            seen_parents.add(grand.table_id)
            for col in columns[:3]:
                dim_id = (
                    f"d_{_suffix(fact.table_id)}_{_suffix(grand.table_id)}_"
                    f"{col.physical}"
                ).lower()
                join_path = [
                    f"{fact.table_id} -> {first.parent}",
                    f"{first.parent} -> {grand.table_id}",
                ]
                is_pref = bool(LABEL_PREF.search(col.label or ""))
                dim_selected = bool(selected and is_pref)
                dimensions.append({
                    "id": dim_id,
                    "name_zh": _dim_name(col.label, grand.label),
                    "column": f"{grand.table_id}.{col.physical}",
                    "filter_column": f"{grand.table_id}.{col.physical}",
                    "filter_lookup": f"{grand.table_id}.{col.physical}",
                    "value_source": grand.table_id,
                    "join_path": join_path,
                    "synonyms": _dim_synonyms(col.label, grand.label),
                    "selected": dim_selected,
                    "provenance": "auto",
                    "confidence": 0.75,
                })
                candidates.append({
                    "id": dim_id,
                    "name_zh": _dim_name(col.label, grand.label),
                    "kind": "categorical",
                    "table": grand.table_id,
                    "column": col.physical,
                    "join_path": join_path,
                    "synonyms": _dim_synonyms(col.label, grand.label),
                    "selected": dim_selected,
                    "confidence": 0.75,
                })
    return dimensions, candidates


def build_draft(dataset_id: str, name: str, tables: list[TableInfo],
                model: ModelSuggestion) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    table_map = {t.table_id: t for t in tables}
    facts = [table_map[t] for t in model.facts if t in table_map and table_map[t].rows]
    if not facts:
        facts = [t for t in tables if t.rows][:1]

    joins: dict[str, dict[str, Any]] = {}
    best_relation: dict[tuple[str, str], RelationInfo] = {}
    for relation in model.relations:
        if not relation.accepted:
            continue
        key = (relation.child, relation.parent)
        current = best_relation.get(key)
        if current is None or relation.score > current.score:
            best_relation[key] = relation
    for (child, parent), relation in best_relation.items():
        joins.setdefault(child, {})[parent] = {
            "left": relation.child_col,
            "right": relation.parent_col,
            "card": "N:1",
        }

    all_dimensions: dict[str, dict[str, Any]] = {}
    all_dim_candidates: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    metric_candidates: list[dict[str, Any]] = []
    time_ranges: list[tuple[str, str]] = []
    month_added = False

    def _primary_rank(table: TableInfo) -> tuple[int, int]:
        label = _short_label(table.label).lower()
        if label in {"orders", "order_items"} or label.endswith("_orders"):
            return (0, -table.rows)
        if re.search(r"order|transaction|item|fact", label):
            return (1, -table.rows)
        return (2, -table.rows)

    primary_facts = {
        t.table_id for t in sorted(facts, key=_primary_rank)[:2]
    }

    for fact in facts:
        fact.role = "fact"
        is_primary = fact.table_id in primary_facts
        dims, dim_candidates = _fact_dimensions(
            fact, model.relations, table_map, selected=is_primary
        )
        tdims, tdim_candidates = _transitive_dimensions(
            fact, model.relations, table_map, selected=is_primary
        )
        dims.extend(tdims)
        dim_candidates.extend(tdim_candidates)
        time_source = _find_time_source(fact, joins, table_map)
        time_table = time_source[0] if time_source else None
        time_col = time_source[1] if time_source else None
        if time_col is not None:
            if not month_added:
                all_dimensions["month"] = {
                    "id": "month",
                    "name_zh": "月份",
                    "kind": "time",
                    "format": "%Y-%m",
                    "column_from": f"{time_table.table_id}.{time_col.physical}",
                    "synonyms": ["月份", "按月", "月度", "month"],
                    "selected": True,
                    "provenance": "auto",
                    "confidence": 0.9,
                }
                all_dim_candidates.append({
                    "id": "month", "name_zh": "月份", "kind": "time",
                    "table": fact.table_id, "column": time_col.physical,
                    "selected": True, "confidence": 0.9,
                })
                month_added = True
            if time_col.minimum and time_col.maximum:
                time_ranges.append((_date_only(time_col.minimum),
                                    _exclusive_end(time_col.maximum)))
        dim_ids = [d["id"] for d in dims]
        supported = ["none"] + (["month"] if time_col else []) + dim_ids

        for dim in dims:
            all_dimensions.setdefault(dim["id"], dim)
        all_dim_candidates.extend(dim_candidates)

        time_via = (
            f"{time_table.table_id}.{time_col.physical}"
            if time_col is not None and time_table is not None else None
        )
        row_metric = _metric(
            fact, fact.table_id, "row_count", "记录数",
            "COUNT(*)", "aggregate", supported, time_via,
            additive=True, unit="行", confidence=0.95,
            definition=f"{fact.label}的记录数（自动口径，未确认）。",
            agg="count", selected=is_primary,
            synonyms=["行数", "记录数", "数据行数"],
        )
        metrics.append(row_metric)
        metric_candidates.append({
            "id": row_metric["id"], "name_zh": row_metric["name_zh"],
            "name": row_metric["name_zh"], "aggregation": "COUNT",
            "fact": fact.table_id, "expr": row_metric["expr"],
            "synonyms": row_metric.get("synonyms"),
            "confidence": 0.95, "selected": is_primary,
            "provenance": "auto",
        })

        # Countable identifiers: product_id, order_id, customer_unique_id...
        for col in fact.columns:
            if col.is_pii or col.inferred_type not in {"varchar", "integer"}:
                continue
            label = _qlabel(col.label)
            if not COUNT_ID_RE.search(label) or NON_COUNT_ID.search(label):
                continue
            if (col.distinct or 0) < 2:
                continue
            name = _distinct_name(label)
            metric = _metric(
                fact, fact.table_id, col.physical, name,
                f"COUNT(DISTINCT {fact.table_id}.{col.physical})",
                "aggregate", supported, time_via,
                additive=False, unit="个", confidence=0.9,
                definition=f"{fact.label}.{label} 的去重计数（自动口径，未确认）。",
                agg="distinct", selected=is_primary,
                synonyms=_synonyms(label, "distinct"),
            )
            metrics.append(metric)
            metric_candidates.append(_candidate(metric, col, "COUNT DISTINCT"))

        for col in fact.columns:
            if col.physical in set(fact.grain) or col.is_pii:
                continue
            if col.inferred_type not in {"integer", "decimal"}:
                continue
            label = _qlabel(col.label)
            if ID_LIKE.search(label) or NON_COUNT_ID.search(col.physical):
                continue
            is_rate = bool(RATE_HINTS.search(label)) or col.unit == "%"
            is_score = bool(SCORE_HINTS.search(label))
            is_measure = bool(
                MEASURE_HINTS.search(label)
                or COUNT_HINTS.search(label)
                or is_score
                or is_rate
                or col.unit in {"元", "%"}
            )
            if not is_measure:
                continue
            unit = _measure_unit(label, col.unit, is_rate, is_score)
            if is_rate or is_score:
                agg = "avg"
                metric = _metric(
                    fact, fact.table_id, col.physical,
                    _measure_name(label, agg, is_score),
                    f"AVG({fact.table_id}.{col.physical})",
                    "aggregate", supported, time_via,
                    additive=False, unit=unit,
                    confidence=round(col.confidence * 0.9, 4),
                    definition=f"{fact.label}.{label} 的平均值（自动口径，未确认）。",
                    agg=agg, selected=is_primary,
                    synonyms=_synonyms(label, agg),
                )
                metrics.append(metric)
                metric_candidates.append(_candidate(metric, col, "AVG"))
                continue
            agg = "sum"
            metric = _metric(
                fact, fact.table_id, col.physical,
                _measure_name(label, agg, False),
                f"SUM({fact.table_id}.{col.physical})",
                "aggregate", supported, time_via,
                additive=True, unit=unit,
                confidence=round(col.confidence * 0.9, 4),
                definition=f"{fact.label}.{label} 的合计（自动口径，未确认）。",
                agg=agg, selected=is_primary,
                synonyms=_synonyms(label, agg),
            )
            metrics.append(metric)
            metric_candidates.append(_candidate(metric, col, "SUM"))

    _add_entity_metrics(facts, primary_facts, tables, joins, metrics, metric_candidates)
    _canonicalize(metrics, primary_facts, table_map)
    _expand_distinct_grains(metrics, all_dimensions, joins)
    selected_by_id = {m["id"]: m.get("selected", True) for m in metrics}
    for candidate in metric_candidates:
        if candidate["id"] in selected_by_id:
            candidate["selected"] = selected_by_id[candidate["id"]]

    for table in tables:
        if table.rows and table.role == "unknown":
            table.role = "dimension" if table.table_id in {d for d in model.dimensions} else "standalone"

    semantic = _semantic_dict(name, tables, facts, joins, all_dimensions, metrics, time_ranges)
    binding = _binding_dict(tables)

    draft = {
        "status": "draft",
        "dataset_id": dataset_id,
        "dataset": name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "facts": [f.table_id for f in facts],
        "dimensions": list(all_dimensions.values()),
        "dimension_candidates": all_dim_candidates,
        "relations": [r.as_dict() for r in model.relations],
        "metrics": metric_candidates,
        "tables": [t.as_dict() for t in tables],
        "warnings": _draft_warnings(tables, model),
        "semantic_version": 1,
        "provenance": "auto",
    }
    # Keep the full metric list for UI review (including row count / distinct).
    for metric in metrics:
        if not any(m["id"] == metric["id"] for m in draft["metrics"]):
            draft["metrics"].append({
                "id": metric["id"],
                "name_zh": metric["name_zh"],
                "name": metric["name_zh"],
                "aggregation": "COUNT",
                "fact": metric["fact"],
                "expr": metric["expr"],
                "confidence": metric.get("confidence", 0.9),
                "selected": True,
                "provenance": "auto",
            })
    dedupe_selected(semantic, draft)
    return semantic, binding, draft


PROVENANCE_RANK = {"documentation": 5, "human": 5, "llm": 3, "auto": 1}


def dedupe_selected(semantic: dict[str, Any],
                    draft: dict[str, Any] | None = None) -> int:
    """Keep exactly one selected metric per display name.

    Auto rules can create the same business name on several facts (订单数 on
    orders and order_items). Prefer explicit/document/LLM definitions over
    name heuristics, then propagate the choice to the draft candidates.
    """
    metrics = semantic.get("metrics") or {}
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for metric_id, metric in metrics.items():
        groups.setdefault(str(metric.get("name_zh") or metric_id), []).append(
            (metric_id, metric)
        )
    changed = 0
    for items in groups.values():
        if len(items) < 2:
            continue
        if not any(bool(m.get("selected", True)) for _, m in items):
            continue

        def rank(item: tuple[str, dict[str, Any]]):
            _mid, metric = item
            return (
                PROVENANCE_RANK.get(str(metric.get("provenance") or "auto"), 1),
                1 if bool(metric.get("selected", True)) else 0,
                float(metric.get("confidence") or 0),
            )

        best_id = max(items, key=rank)[0]
        for metric_id, metric in items:
            wanted = metric_id == best_id
            if bool(metric.get("selected", True)) != wanted:
                metric["selected"] = wanted
                changed += 1
    if draft is not None:
        flags = {
            mid: bool(m.get("selected", True)) for mid, m in metrics.items()
        }
        for item in draft.get("metrics") or []:
            if item.get("id") in flags:
                item["selected"] = flags[item["id"]]
    return changed


def _date_only(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return str(value)


def _exclusive_end(value) -> str:
    """Semantic ranges are [start, end); include the last observed day."""
    if isinstance(value, datetime):
        return (value.date() + timedelta(days=1)).isoformat()
    if isinstance(value, date):
        return (value + timedelta(days=1)).isoformat()
    try:
        parsed = date.fromisoformat(str(value)[:10])
        return (parsed + timedelta(days=1)).isoformat()
    except ValueError:
        return str(value)


def _find_time_source(fact: TableInfo, joins: dict[str, dict[str, Any]],
                      table_map: dict[str, TableInfo]):
    own = _time_column(fact)
    if own is not None:
        return fact, own
    direct = list((joins.get(fact.table_id) or {}).keys())
    candidates = []
    for parent_id in direct:
        parent = table_map.get(parent_id)
        if parent is None:
            continue
        col = _time_column(parent)
        if col is not None:
            candidates.append((parent, col))
    if candidates:
        candidates.sort(key=lambda item: (
            0 if re.search(r"order", item[0].label or "", re.I) else 1,
            -item[0].rows,
        ))
        return candidates[0]
    for parent_id in direct:
        for grand_id in (joins.get(parent_id) or {}):
            grand = table_map.get(grand_id)
            if grand is None:
                continue
            col = _time_column(grand)
            if col is not None:
                return grand, col
    return None


def _time_column(fact: TableInfo):
    dates = [c for c in fact.columns if c.inferred_type in {"date", "datetime"}]
    if not dates:
        return None
    dates.sort(key=lambda c: (
        0 if re.search(r"日期|时间|date|time|day|dt", c.label or "", re.I) else 1,
        -(c.distinct or 0),
    ))
    return dates[0]


def _synonyms(label: str, agg: str) -> list[str]:
    out = [label]
    for pattern, aliases in SYNONYM_RULES:
        if pattern.search(label or ""):
            out.extend(aliases)
    if agg == "distinct" and len(out) == 1:
        out.append(f"{label}去重数")
    elif agg == "sum":
        out.append(f"{label}合计")
    elif agg == "avg":
        out.append(f"{label}平均值")
    return list(dict.fromkeys(out))


def _distinct_name(label: str) -> str:
    for pattern, aliases in SYNONYM_RULES:
        if pattern.search(label or "") and aliases:
            return aliases[0]
    return f"{label}去重数"


def _measure_name(label: str, agg: str, is_score: bool) -> str:
    if is_score:
        return "平均评分" if agg == "avg" else "评分合计"
    low = (label or "").lower()
    if agg == "sum":
        if low == "price":
            return "商品销售额"
        if "freight" in low:
            return "运费合计"
        if "payment_value" in low:
            return "支付金额"
        if "base_price" in low:
            return "商品标价合计"
        return f"{label}合计"
    if "率" in label:
        return label
    return f"平均{label}"


def _measure_unit(label: str, unit: str | None, is_rate: bool, is_score: bool) -> str | None:
    if is_score:
        return unit or "分"
    if is_rate:
        return unit or "%"
    if unit:
        return unit
    low = (label or "").lower()
    if any(k in low for k in ("price", "amount", "value", "freight", "revenue", "sales")):
        return "元"
    if COUNT_HINTS.search(label or ""):
        return "件"
    return None


ENTITY_NAMES = {
    "categories": "品类数",
    "category": "品类数",
    "products": "商品数",
    "product": "商品数",
    "customers": "客户数",
    "customer": "客户数",
    "sellers": "商家数",
    "seller": "商家数",
    "users": "用户数",
}


def _reachable(joins: dict[str, dict[str, Any]], start: str, target: str) -> bool:
    graph: dict[str, set[str]] = {}
    for child, links in joins.items():
        for parent in links:
            graph.setdefault(child, set()).add(parent)
            graph.setdefault(parent, set()).add(child)
    seen, queue = {start}, [start]
    while queue:
        node = queue.pop()
        if node == target:
            return True
        for nxt in graph.get(node, set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def _reachable_tables(joins: dict[str, dict[str, Any]], start: str) -> set[str]:
    graph: dict[str, set[str]] = {}
    for child, links in joins.items():
        for parent in links:
            graph.setdefault(child, set()).add(parent)
            graph.setdefault(parent, set()).add(child)
    seen, queue = {start}, [start]
    while queue:
        node = queue.pop()
        for nxt in graph.get(node, set()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _expand_distinct_grains(metrics: list[dict[str, Any]],
                            dimensions: dict[str, dict[str, Any]],
                            joins: dict[str, dict[str, Any]]) -> None:
    """COUNT(DISTINCT ...) survives fan-out; expose dimensions across the graph.

    SUM/AVG stay on N:1-only dimensions to avoid join inflation.
    """
    for metric in metrics:
        expr = str(metric.get("expr") or "").upper()
        if not expr.startswith("COUNT(DISTINCT"):
            continue
        reach = _reachable_tables(joins, str(metric.get("fact")))
        supported = list(metric.get("supported_grain") or ["none"])
        for dim_id, dim in dimensions.items():
            if dim_id in supported:
                continue
            if dim.get("kind") in {"time", "derived"}:
                continue
            column = str(dim.get("column") or "")
            table = column.split(".", 1)[0] if "." in column else ""
            if table and table in reach:
                supported.append(dim_id)
        metric["supported_grain"] = supported


def _add_entity_metrics(facts: list[TableInfo], primary_facts: set[str],
                        tables: list[TableInfo], joins: dict[str, dict[str, Any]],
                        metrics: list[dict[str, Any]],
                        candidates: list[dict[str, Any]]) -> None:
    primary = next((f for f in facts if f.table_id in primary_facts), facts[0] if facts else None)
    if primary is None:
        return
    fact_metrics = [m for m in metrics if m.get("fact") == primary.table_id]
    if not fact_metrics:
        return
    supported = list(fact_metrics[0].get("supported_grain") or ["none"])
    table_map = {t.table_id: t for t in tables}
    time_source = _find_time_source(primary, joins, table_map)
    time_via = (
        f"{time_source[0].table_id}.{time_source[1].physical}"
        if time_source else None
    )
    existing_names = {m["name_zh"] for m in metrics}
    for table in tables:
        if table.table_id in {f.table_id for f in facts} or len(table.grain) != 1:
            continue
        if re.search(r"geo|location|坐标|经纬", table.label or "", re.I):
            continue
        pk = table.grain[0]
        if not _label_columns(table, pk):
            continue  # geo/code-like tables without a name label
        if not _reachable(joins, primary.table_id, table.table_id):
            continue
        short = _short_label(table.label)
        name = ENTITY_NAMES.get(short.lower(), f"{short}数")
        if name in existing_names:
            continue
        metric_id = f"m_{_suffix(primary.table_id)}_{_suffix(table.table_id)}_distinct".lower()
        if metric_id in {m["id"] for m in metrics}:
            continue
        base = name[:-1] if name.endswith("数") else name
        synonyms = list(dict.fromkeys([
            name, f"几种{base}", f"多少种{base}",
        ]))
        metric = {
            "id": metric_id,
            "name_zh": name,
            "type": "aggregate",
            "fact": primary.table_id,
            "expr": f"COUNT(DISTINCT {table.table_id}.{pk})",
            "scope": {},
            "time_via": time_via,
            "additive": False,
            "unit": "个",
            "supported_grain": supported,
            "synonyms": synonyms,
            "definition": f"{short}中不同主键的去重计数（自动口径，未确认）。",
            "selected": True,
            "provenance": "auto",
            "confidence": 0.8,
        }
        metrics.append(metric)
        candidates.append({
            "id": metric_id,
            "name_zh": name,
            "name": name,
            "aggregation": "COUNT DISTINCT",
            "fact": primary.table_id,
            "expr": metric["expr"],
            "synonyms": synonyms,
            "confidence": 0.8,
            "selected": True,
            "provenance": "auto",
        })
        existing_names.add(name)


def _canonicalize(metrics: list[dict[str, Any]],
                  primary_facts: set[str],
                  table_map: dict[str, TableInfo]) -> None:
    """When the same business name exists on several facts, pick one default."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for metric in metrics:
        groups.setdefault(metric["name_zh"], []).append(metric)

    def score(metric: dict[str, Any]) -> float:
        fact = table_map.get(metric.get("fact"))
        label = _short_label(fact.label).lower() if fact else ""
        name = metric["name_zh"]
        value = 2.0 if metric.get("selected") else 0.0
        value += float(metric.get("confidence") or 0)
        if name == "订单数" and "orders" in label:
            value += 5
        if name in {"商品数", "商家数"} and "order_items" in label:
            value += 5
        if name == "客户数" and "orders" in label:
            value += 5
        if name == "记录数":
            value += 5 if metric.get("fact") in primary_facts else 0
            value += (fact.rows / 1000.0) if fact else 0
        return value

    for group in groups.values():
        if len(group) < 2:
            continue
        best = max(group, key=score)
        for metric in group:
            metric["selected"] = metric is best


def _metric(fact: TableInfo, table_id: str, col: str, label: str, expr: str,
            metric_type: str, supported: list[str], time_via: str | None,
            *, additive: bool, unit: str | None, confidence: float,
            definition: str, agg: str, selected: bool = True,
            synonyms: list[str] | None = None) -> dict[str, Any]:
    if agg == "count" and col == "row_count":
        metric_id = f"m_{_suffix(table_id)}_row_count".lower()
    else:
        metric_id = f"m_{_suffix(table_id)}_{col}_{agg}".lower()
    base = re.sub(r"^平均", "", label)
    base = re.sub(r"(合计|平均值|去重数|行数|记录数)$", "", base) or label
    synonyms = list(dict.fromkeys([*(synonyms or []), label, base]))
    return {
        "id": metric_id,
        "name_zh": label,
        "synonyms": synonyms,
        "type": metric_type,
        "fact": fact.table_id,
        "expr": expr,
        "scope": {},
        "time_via": time_via,
        "additive": additive,
        "unit": unit,
        "supported_grain": list(supported),
        "definition": definition,
        "selected": bool(selected),
        "provenance": "auto",
        "confidence": round(confidence, 4),
    }


def _candidate(metric: dict[str, Any], col, aggregation: str) -> dict[str, Any]:
    return {
        "id": metric["id"],
        "name_zh": metric["name_zh"],
        "name": metric["name_zh"],
        "aggregation": aggregation,
        "fact": metric["fact"],
        "expr": metric["expr"],
        "unit": metric.get("unit"),
        "synonyms": metric.get("synonyms"),
        "confidence": metric.get("confidence", 0.9),
        "selected": metric.get("selected", True),
        "provenance": "auto",
        "evidence": {
            "column": col.physical,
            "label": col.label,
            "null_rate": col.null_rate,
            "distinct": col.distinct,
            "samples": col.samples[:5],
        },
    }


def _semantic_dict(name: str, tables: list[TableInfo], facts: list[TableInfo],
                   joins: dict[str, dict[str, Any]],
                   dimensions: dict[str, dict[str, Any]],
                   metrics: list[dict[str, Any]],
                   time_ranges: list[tuple[str, str]]) -> dict[str, Any]:
    time_range = ["1900-01-01", "2100-01-01"]
    if time_ranges:
        time_range = [min(t[0] for t in time_ranges), max(t[1] for t in time_ranges)]
    return {
        "version": 1,
        "dataset": name,
        "dataset_id": None,
        "compiler_version": 1,
        "time_range": time_range,
        "default_year": "latest_occurrence",
        "status": "draft",
        "provenance": "auto",
        "parser": "generic",
        "capabilities": {
            "member_list": True,
            "ratio": True,
            "entity_count": True,
            "compare": True,
            "business_checks": [],
            "member_list_coverage": None,
        },
        "tables": {
            table.table_id: {
                "grain": table.grain or [],
                "time": _time_column(table).physical if _time_column(table) else None,
                "columns": [c.physical for c in table.columns],
            }
            for table in tables if table.rows
        },
        "joins": joins,
        "pre_aggregates": {},
        "metrics": {m["id"]: {k: v for k, v in m.items() if k != "id"} for m in metrics},
        "dimensions": dimensions,
        "policies": {
            "ambiguity": "ask_once_then_remember",
            "missing_year": "latest_occurrence_in_range",
            "ratio_grain": "order",
            "max_retry": 1,
        },
    }


def _binding_dict(tables: list[TableInfo]) -> dict[str, Any]:
    return {
        "version": 1,
        "tables": {
            table.table_id: {c.physical: c.physical for c in table.columns}
            for table in tables if table.rows
        },
    }


def _draft_warnings(tables: list[TableInfo], model: ModelSuggestion) -> list[str]:
    warnings: list[str] = []
    rejected = [r for r in model.relations if not r.accepted]
    if rejected:
        warnings.append(f"{len(rejected)} 个候选关系未通过连接放大/名称校验，未进入草稿")
    for table in tables:
        if table.rows and not table.grain:
            warnings.append(f"{table.label} 没有唯一主键，行数类指标使用整行粒度")
        if table.totals_rows:
            warnings.append(f"{table.label} 含 {len(table.totals_rows)} 个疑似合计行，已从分析表排除")
    if not any(c.inferred_type in {"date", "datetime"} for t in tables for c in t.columns):
        warnings.append("未识别到日期列，时间趋势与月份维度不可用")
    return warnings


def write_draft(root: Path, semantic: dict[str, Any], binding: dict[str, Any],
                draft: dict[str, Any]) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    semantic_path = root / "semantic.yaml"
    binding_path = root / "binding.yaml"
    draft_path = root / "semantic_draft.json"
    semantic_path.write_text(
        yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    binding_path.write_text(
        yaml.safe_dump(binding, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    import json
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return {"semantic": semantic_path, "binding": binding_path, "draft": draft_path}
