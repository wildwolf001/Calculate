"""Parse a README / metrics document into authoritative semantic rules.

The document is treated as user-supplied data, not as code. Rules are mapped
onto existing physical columns, validated against the warehouse, and applied
only as a draft; publishing remains an explicit human action.
"""
from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from app.datasets.context import DatasetContext
from app.domain.semantic.loader import load_semantic

SUPPORTED_DOC_SUFFIXES = {".md", ".markdown", ".txt", ".json", ".yaml", ".yml"}
MAX_DOC_BYTES = 2 * 1024 * 1024

SKIP_KEYS = re.compile(
    r"^(数据范围|数据结构|数据来源|随机种子|说明|字段|表结构|文件|版本|更新|作者|备注)$",
    re.I,
)
METADATA_HINTS = re.compile(r"随机种子|数据结构|数据来源|生成|可重复|文件说明", re.I)
STATUS_ALIASES = {
    "已送达": "delivered",
    "已取消": "cancelled",
    "已发货": "shipped",
    "运输中": "shipped",
    "不可用": "unavailable",
    "已支付": "paid",
    "已付款": "paid",
}
SUM_NAME_HINTS = re.compile(r"销售额|金额|收入|运费|价格|成本|利润|GMV", re.I)
COUNT_NAME_HINTS = re.compile(r"订单数|客户数|商品数|商家数|评价数|数量|件数|人数|几种|个数", re.I)
RATIO_HINTS = re.compile(r"率|比例|占比", re.I)
FORMULA_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|>|<|=)\s*([A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?)"
)
TABLE_HINTS = [
    (re.compile(r"订单数|订单量|订单", re.I),
     ["orders", "order_items"]),
    (re.compile(r"商品|产品|单品", re.I),
     ["order_items", "products"]),
    (re.compile(r"客户|买家|用户", re.I),
     ["customers", "orders"]),
    (re.compile(r"商家|卖家|店铺", re.I),
     ["sellers", "order_items"]),
    (re.compile(r"评价|评分|低分|差评", re.I),
     ["order_reviews"]),
    (re.compile(r"支付|付款", re.I),
     ["order_payments"]),
    (re.compile(r"品类|类目", re.I),
     ["categories", "products"]),
]


class DocumentationError(RuntimeError):
    pass


@dataclass
class CatalogIndex:
    dataset: DatasetContext
    table_by_label: dict[str, str] = field(default_factory=dict)
    label_by_table: dict[str, str] = field(default_factory=dict)
    columns: dict[str, dict[str, str]] = field(default_factory=dict)
    columns_by_label: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    types: dict[str, dict[str, str]] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)

    @classmethod
    def build(cls, ctx: DatasetContext) -> "CatalogIndex":
        index = cls(dataset=ctx)
        source = {}
        source_path = ctx.root / "source.yaml"
        if source_path.exists():
            try:
                source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                source = {}
        labels = {
            tid: (spec or {}).get("label", tid)
            for tid, spec in (source.get("tables") or {}).items()
        }
        for tid, table in (ctx.catalog.get("tables") or {}).items():
            label = labels.get(tid) or tid
            index.table_by_label[label.lower()] = tid
            index.table_by_label[label] = tid
            index.label_by_table[tid] = label
            index.rows[tid] = int(table.get("rows") or 0)
            cols: dict[str, str] = {}
            types: dict[str, str] = {}
            for col in table.get("columns") or []:
                label_col = str(col.get("label") or col.get("name"))
                physical = str(col.get("name"))
                cols[label_col.lower()] = physical
                cols[physical.lower()] = physical
                types[physical] = str(col.get("type") or "")
                index.columns_by_label.setdefault(label_col.lower(), []).append((tid, physical))
            index.columns[tid] = cols
            index.types[tid] = types
        return index

    def find_table(self, value: str | None) -> str | None:
        if not value:
            return None
        return self.table_by_label.get(str(value)) or self.table_by_label.get(str(value).lower())

    def find_column(self, table_id: str | None, value: str) -> tuple[str, str] | None:
        value_l = str(value).lower()
        if table_id:
            physical = self.columns.get(table_id, {}).get(value_l)
            if physical:
                return table_id, physical
        hits = self.columns_by_label.get(value_l) or []
        if len(hits) == 1:
            return hits[0]
        return None

    def all_columns(self) -> list[str]:
        return sorted(self.columns_by_label)


def _clean_line(line: str) -> str:
    return re.sub(r"^\s*(?:[-*+#]+\s*)+", "", line).strip()


def _normalize_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"(口径|定义)$", "", text).strip()
    return text or str(name)


def _extract_backticks(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"`([^`]+)`", text)]


def _extract_known_columns(text: str, index: CatalogIndex) -> list[str]:
    found: list[str] = []
    low = text.lower()
    for label in index.all_columns():
        if len(label) >= 3 and re.search(rf"(?<![a-z0-9_]){re.escape(label)}(?![a-z0-9_])", low):
            found.append(label)
    for token in _extract_backticks(text):
        token_l = token.lower()
        if token_l in index.columns_by_label and token not in found:
            found.append(token)
    return list(dict.fromkeys(found))


def _extract_table(text: str, index: CatalogIndex) -> str | None:
    for label in sorted(index.table_by_label, key=len, reverse=True):
        if len(label) >= 3 and re.search(rf"(?<![a-z0-9_]){re.escape(label)}(?![a-z0-9_])", text.lower()):
            return index.table_by_label[label]
    return None


def _guess_aggregation(name: str, definition: str, columns: list[str],
                       formula: str | None) -> str | None:
    text = f"{name} {definition}"
    if RATIO_HINTS.search(name) and ("/" in definition or formula):
        return "ratio"
    if formula and RATIO_HINTS.search(name):
        return "ratio"
    if formula:
        return "count"
    if re.search(r"去重|几种|多少个|多少种|人数|客户数|商品数|商家数|订单数", text, re.I):
        return "count_distinct"
    if re.search(r"合计|求和|总和|总金额|sum", text, re.I):
        return "sum"
    if re.search(r"平均|均值|avg", text, re.I):
        return "avg"
    if SUM_NAME_HINTS.search(name) or SUM_NAME_HINTS.search(definition):
        return "sum"
    if COUNT_NAME_HINTS.search(name):
        return "count"
    return None


def _guess_filters(definition: str) -> tuple[dict[str, str], list[str]]:
    filters: dict[str, str] = {}
    notes: list[str] = []
    if re.search(r"已送达|delivered", definition, re.I):
        filters["order_status"] = "delivered"
    if re.search(r"不含运费|exclude.*freight", definition, re.I):
        notes.append("不含运费")
    if re.search(r"已取消|cancelled", definition, re.I):
        filters["order_status"] = "cancelled"
    return filters, notes


def deterministic_rules(text: str, index: CatalogIndex) -> tuple[list[dict[str, Any]], list[str]]:
    rules: list[dict[str, Any]] = []
    notes: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line or len(line) < 3:
            continue
        if METADATA_HINTS.search(line):
            notes.append(line)
            continue
        match = re.match(r"^([^：:]{2,40})[：:]\s*(.+)$", line)
        if not match:
            continue
        name, definition = match.group(1).strip(), match.group(2).strip()
        name = _normalize_name(name)
        if SKIP_KEYS.match(name):
            notes.append(line)
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:元|笔|个|人|条|%|单|行)?", definition) or re.match(
            r"^\d{4}[-/年]\d{1,2}", definition
        ):
            notes.append(line)
            continue
        columns = _extract_known_columns(definition, index)
        if not columns and RATIO_HINTS.search(name):
            hinted = []
            if "延迟" in name:
                hinted = ["delivered_at", "estimated_delivery_at", "order_id"]
            elif "低分" in name or "差评" in name:
                hinted = ["review_score", "review_id"]
            columns = [c for c in hinted if c.lower() in index.columns_by_label]
        table = _extract_table(definition, index)
        formula_match = FORMULA_RE.search(definition)
        formula = formula_match.group(0) if formula_match else None
        if not formula and RATIO_HINTS.search(name):
            low_columns = {c.lower() for c in columns}
            if "延迟" in name and {"delivered_at", "estimated_delivery_at"} <= low_columns:
                formula = "delivered_at > estimated_delivery_at"
            elif ("低分" in name or "差评" in name) and "review_score" in low_columns:
                formula = "review_score <= 2"
        aggregation = _guess_aggregation(name, definition, columns, formula)
        filters, filter_notes = _guess_filters(definition)
        aliases: list[str] = []
        alias_match = re.search(r"[（(]([^）)]+)[）)]", name)
        if alias_match and re.search(r"同义|又称|也叫|简称|别名", alias_match.group(1)):
            aliases = [x.strip() for x in re.split(r"[,，、/]", alias_match.group(1)) if x.strip()]
        rule = {
            "name": name,
            "aliases": aliases,
            "definition": definition,
            "aggregation": aggregation,
            "table": index.label_by_table.get(table) if table else None,
            "columns": columns,
            "filters": filters,
            "formula": formula,
            "unit": "%" if aggregation == "ratio" or "%" in definition else None,
            "notes": filter_notes,
            "raw": line,
            "source": "document",
        }
        if not columns and not formula and aggregation is None:
            notes.append(f"未解析出列或聚合：{line}")
            continue
        rules.append(rule)
    return rules, notes


def _llm_rules(text: str, index: CatalogIndex) -> tuple[list[dict[str, Any]], list[str]]:
    from app.core.logging import log_event, logger
    from app.llm.client import LLMClient

    # Reasoning models can burn a small budget before emitting any text.
    client = LLMClient(timeout=120.0)
    if not client.available:
        return [], []
    catalog_lines = []
    for tid, label in index.label_by_table.items():
        cols = []
        for col_label, physical in list(index.columns.get(tid, {}).items())[:40]:
            if col_label == physical.lower():
                continue
            cols.append(f"{col_label}({physical})")
        catalog_lines.append(f"- {label} [{tid}] 列: " + ", ".join(cols[:30]))
    system = (
        "你是数据口径解析器。只输出 JSON，不要输出 SQL 或解释。"
        "只能使用给出的表和列，不能发明新的表/列/取值。"
        "每个指标输出 name, aliases, definition, aggregation, table, columns, "
        "filters, formula, unit。aggregation 只能是 sum/avg/count/count_distinct/ratio 或 null。"
        "filters 是列名到取值的对象。formula 是原文中的比较式，如 delivered_at > estimated_delivery_at。"
    )
    user = (
        "表结构：\n" + "\n".join(catalog_lines)
        + "\n\n口径文档：\n" + text[:12000]
        + "\n\n输出：{\"metrics\": [...]}"
    )
    try:
        data = client.complete_json(system, user, max_tokens=8000, stage="documentation")
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log_event(logger, "documentation.llm_failed", error=reason[:500])
        return [], [f"LLM 解析失败（{type(exc).__name__}），使用确定性解析"]
    rules = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return [], ["LLM 输出格式不正确，使用确定性解析"]
    out: list[dict[str, Any]] = []
    for item in rules:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        out.append({
            "name": _normalize_name(str(item.get("name"))),
            "aliases": [str(x) for x in (item.get("aliases") or [])],
            "definition": str(item.get("definition") or ""),
            "aggregation": item.get("aggregation") if item.get("aggregation") in
            {"sum", "avg", "count", "count_distinct", "ratio"} else None,
            "table": item.get("table"),
            "columns": [str(x) for x in (item.get("columns") or [])],
            "filters": item.get("filters") if isinstance(item.get("filters"), dict) else {},
            "formula": item.get("formula"),
            "unit": item.get("unit"),
            "notes": [],
            "raw": "",
            "source": "llm",
        })
    return out, []


def _merge_rules(det: list[dict[str, Any]], llm: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not llm:
        return det
    merged: dict[str, dict[str, Any]] = {}
    for rule in det:
        merged[rule["name"].lower()] = rule
    for rule in llm:
        key = rule["name"].lower()
        if key in merged:
            base = merged[key]
            base["aliases"] = list(dict.fromkeys(base.get("aliases", []) + rule.get("aliases", [])))
            base["definition"] = rule.get("definition") or base.get("definition")
            base["aggregation"] = rule.get("aggregation") or base.get("aggregation")
            base["table"] = rule.get("table") or base.get("table")
            base["columns"] = list(dict.fromkeys(rule.get("columns") or base.get("columns") or []))
            base["filters"] = {**(base.get("filters") or {}), **(rule.get("filters") or {})}
            base["formula"] = rule.get("formula") or base.get("formula")
            base["unit"] = rule.get("unit") or base.get("unit")
            base["source"] = "llm+document"
        else:
            merged[key] = rule

    # Two sources often describe the same formula with slightly different
    # names ("延迟订单" / "延迟订单数"). Keep one rule per formula.
    deduped: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    for rule in merged.values():
        if rule.get("formula"):
            sig = (rule.get("table"), rule.get("formula"), rule.get("aggregation"))
        else:
            sig = (
                rule.get("table"),
                rule.get("aggregation"),
                tuple(sorted(str(c) for c in (rule.get("columns") or []))),
            )
        if sig in deduped:
            base = deduped[sig]
            base["aliases"] = list(dict.fromkeys(
                base.get("aliases", []) + [rule.get("name", "")] + rule.get("aliases", [])
            ))
            base["filters"] = {**(base.get("filters") or {}), **(rule.get("filters") or {})}
            base["columns"] = list(dict.fromkeys(
                (base.get("columns") or []) + (rule.get("columns") or [])
            ))
        else:
            deduped[sig] = rule
            order.append(sig)
    return [deduped[sig] for sig in order]


def _infer_table(rule: dict[str, Any], index: CatalogIndex) -> str | None:
    labels = [str(c) for c in (rule.get("columns") or [])]
    candidates: list[str] = []
    for label in labels:
        for tid, _physical in index.columns_by_label.get(label.lower(), []):
            if tid not in candidates:
                candidates.append(tid)
    if not candidates:
        return None
    for pattern, table_labels in TABLE_HINTS:
        if not pattern.search(rule.get("name") or ""):
            continue
        for table_label in table_labels:
            tid = index.table_by_label.get(table_label)
            if tid and tid in candidates:
                return tid
    candidates.sort(key=lambda tid: index.rows.get(tid, 0), reverse=True)
    return candidates[0]


def parse_document(text: str, filename: str, ctx: DatasetContext) -> dict[str, Any]:
    index = CatalogIndex.build(ctx)
    warnings: list[str] = []
    notes: list[str] = []

    if filename.lower().endswith(".json"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("metrics"), list):
                # Structured JSON is already a rule document.
                text = "\n".join(
                    f"{m.get('name')}：{m.get('definition') or ''}"
                    for m in data["metrics"] if isinstance(m, dict)
                )
        except json.JSONDecodeError:
            warnings.append("JSON 文档无法解析，按文本处理")

    det, det_notes = deterministic_rules(text, index)
    notes.extend(det_notes)
    llm, llm_notes = _llm_rules(text, index)
    warnings.extend(llm_notes)
    rules = _merge_rules(det, llm)

    validated: list[dict[str, Any]] = []
    for rule in rules:
        table_id = index.find_table(rule.get("table"))
        if not table_id:
            table_id = _infer_table(rule, index)
        columns = []
        for col in rule.get("columns") or []:
            hit = index.find_column(table_id, col)
            if hit:
                columns.append({"table": hit[0], "column": hit[1], "label": col})
        if not columns and rule.get("formula"):
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rule["formula"]):
                hit = index.find_column(table_id, token)
                if hit:
                    columns.append({"table": hit[0], "column": hit[1], "label": token})
        if not table_id and columns:
            table_id = columns[0]["table"]
        rule["table_id"] = table_id
        rule["resolved_columns"] = columns
        if not table_id:
            warnings.append(f"无法定位表：{rule['name']}")
            continue
        validated.append(rule)

    return {
        "source": filename,
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
        "parser": "llm+deterministic" if llm else "deterministic",
        "rules": validated,
        "notes": notes,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _aggregation_matches(metric: dict[str, Any], agg: str | None) -> bool:
    if not agg:
        return True
    expr = str(metric.get("expr") or "").upper().replace(" ", "")
    if agg == "sum":
        return expr.startswith("SUM(")
    if agg == "avg":
        return expr.startswith("AVG(")
    if agg == "count_distinct":
        return expr.startswith("COUNT(DISTINCT")
    if agg == "count":
        return expr.startswith("COUNT(") and "DISTINCT" not in expr
    return True


def _find_metric(semantic: dict[str, Any], rule: dict[str, Any],
                 index: CatalogIndex) -> tuple[str, dict[str, Any]] | None:
    columns = rule.get("resolved_columns") or []
    names = {rule["name"].lower(), *[a.lower() for a in rule.get("aliases") or []]}
    agg = rule.get("aggregation")
    best = None
    expr_hits: list[tuple[str, dict[str, Any]]] = []
    for mid, metric in (semantic.get("metrics") or {}).items():
        if not _aggregation_matches(metric, agg):
            continue
        expr = str(metric.get("expr") or "")
        for col in columns:
            ref = f"{col['table']}.{col['column']}"
            if ref in expr:
                expr_hits.append((mid, metric))
                break
        metric_names = {str(metric.get("name_zh", "")).lower(), *[
            str(x).lower() for x in metric.get("synonyms") or []
        ]}
        if names & metric_names and best is None:
            best = (mid, metric)
    if expr_hits:
        selected = [
            item for item in expr_hits
            if item[1].get("selected") or item[1].get("provenance") == "documentation"
        ]
        return (selected or expr_hits)[0]
    return best


def _scope_value(value: str, column_label: str) -> str:
    value = str(value)
    if column_label.lower() == "order_status":
        return STATUS_ALIASES.get(value, value)
    return value


def _find_column_anywhere(index: CatalogIndex, label: str) -> tuple[str, str] | None:
    hits = index.columns_by_label.get(label.lower()) or []
    return hits[0] if len(hits) == 1 else None


def _has_path(semantic: dict[str, Any], start: str, target: str) -> bool:
    graph: dict[str, set[str]] = {}
    for child, links in (semantic.get("joins") or {}).items():
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


def _add_join_if_possible(semantic: dict[str, Any], index: CatalogIndex,
                          fact: str, target: str, ctx: DatasetContext) -> bool:
    if fact == target or _has_path(semantic, fact, target):
        return True
    target_grain = ((semantic.get("tables") or {}).get(target) or {}).get("grain") or []
    if len(target_grain) != 1:
        return False
    parent_key = target_grain[0]
    parent_label = index.columns.get(target, {})
    label_for_key = next(
        (k for k, v in parent_label.items() if v == parent_key and k != parent_key.lower()),
        parent_key,
    )
    candidates = index.columns.get(fact, {})
    for label, physical in candidates.items():
        if label == label_for_key or label == f"{index.label_by_table.get(target, target)}_id":
            semantic.setdefault("joins", {}).setdefault(fact, {})[target] = {
                "left": physical, "right": parent_key, "card": "N:1",
            }
            return True
    return False


def _reachable_tables(semantic: dict[str, Any], start: str) -> set[str]:
    graph: dict[str, set[str]] = {}
    for child, links in (semantic.get("joins") or {}).items():
        for parent in links:
            graph.setdefault(child, set()).add(parent)
            graph.setdefault(parent, set()).add(child)
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        for nxt in graph.get(node, set()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _supported_grain(semantic: dict[str, Any], fact: str,
                     include_reachable: bool = False) -> list[str]:
    """Dimensions a metric may be split by.

    `include_reachable=True` is for ratio metrics: the compiler aggregates to
    the ratio grain first, so dimensions reached through 1:N joins cannot
    double-count the numerator/denominator.
    """
    grains = ["none"]
    reachable = _reachable_tables(semantic, fact) if include_reachable else None
    for dim_id, dim in (semantic.get("dimensions") or {}).items():
        kind = dim.get("kind", "categorical")
        join_path = dim.get("join_path") or []
        column = str(dim.get("column") or "")
        if kind == "time":
            continue
        if reachable is not None:
            if kind == "derived":
                table = str(dim.get("requires") or "")
            else:
                table = column.split(".", 1)[0] if "." in column else ""
            if not table or table not in reachable:
                continue
        else:
            if join_path and join_path[0].split(" -> ")[0] != fact:
                continue
            if not join_path and column and not column.startswith(f"{fact}."):
                continue
        grains.append(dim_id)
    if _time_via(semantic, fact):
        grains.insert(1, "month")
    return grains


def _time_via(semantic: dict[str, Any], fact: str) -> str | None:
    time_col = (semantic.get("tables") or {}).get(fact, {}).get("time")
    if time_col:
        return f"{fact}.{time_col}"
    for parent, spec in ((semantic.get("joins") or {}).get(fact) or {}).items():
        parent_time = (semantic.get("tables") or {}).get(parent, {}).get("time")
        if parent_time:
            return f"{parent}.{parent_time}"
    return None


def _new_metric_id(semantic: dict[str, Any], name: str) -> str:
    base = "m_doc_" + re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", name).strip("_").lower()
    metric_id = base
    counter = 1
    while metric_id in (semantic.get("metrics") or {}):
        counter += 1
        metric_id = f"{base}_{counter}"
    return metric_id


def _map_scope(rule: dict[str, Any], semantic: dict[str, Any],
               index: CatalogIndex, fact: str, ctx: DatasetContext,
               warnings: list[str]) -> dict[str, str]:
    scope: dict[str, str] = {}
    for key, value in (rule.get("filters") or {}).items():
        hit = _find_column_anywhere(index, key)
        if not hit:
            warnings.append(f"{rule['name']}: 找不到筛选列 {key}")
            continue
        table, column = hit
        if not _add_join_if_possible(semantic, index, fact, table, ctx):
            warnings.append(f"{rule['name']}: {fact} 无法关联到 {table}，筛选 {key} 未应用")
            continue
        scope[f"{table}.{column}"] = _scope_value(str(value), key)
    return scope


def _column_type(index: CatalogIndex, table: str, column: str) -> str:
    return index.types.get(table, {}).get(column, "")


def _normalize_unit(unit: Any, agg: str | None = None) -> str | None:
    text = str(unit or "").strip().lower()
    if not text:
        return None
    if text in {"currency", "money", "元", "cny", "rmb", "yuan"}:
        return "元"
    if text in {"percent", "percentage", "%", "rate", "ratio"}:
        return "%"
    if text in {"orders", "order", "笔", "count"}:
        return "笔"
    if text in {"items", "item", "件"}:
        return "件"
    if text in {"people", "person", "人", "customers"}:
        return "人"
    return str(unit).strip()


def _apply_aggregate_rule(rule: dict[str, Any], semantic: dict[str, Any],
                          index: CatalogIndex, ctx: DatasetContext,
                          warnings: list[str]) -> tuple[str | None, str]:
    columns = rule.get("resolved_columns") or []
    if not columns or not rule.get("aggregation"):
        return None, "缺少列或聚合"
    agg = rule["aggregation"]
    if agg in {"sum", "avg"}:
        numeric = [
            c for c in columns
            if _column_type(index, c["table"], c["column"]) in {"integer", "decimal"}
        ]
        if not numeric:
            return None, "找不到数值列"
        columns = numeric + [c for c in columns if c not in numeric]
    fact = columns[0]["table"]
    column = columns[0]["column"]
    if agg == "ratio":
        return None, "比率规则需要明确分子/分母"
    expr_map = {
        "sum": f"SUM({fact}.{column})",
        "avg": f"AVG({fact}.{column})",
        "count": f"COUNT({fact}.{column})",
        "count_distinct": f"COUNT(DISTINCT {fact}.{column})",
    }
    expr = expr_map.get(agg)
    if not expr:
        return None, f"不支持的聚合 {agg}"
    scope = _map_scope(rule, semantic, index, fact, ctx, warnings)
    time_via = _time_via(semantic, fact)
    supported = _supported_grain(semantic, fact)
    find_rule = dict(rule)
    find_rule["resolved_columns"] = [columns[0]]
    found = _find_metric(semantic, find_rule, index)
    if found:
        metric_id, metric = found
        action = "updated"
    else:
        metric_id = _new_metric_id(semantic, rule["name"])
        metric = {
            "fact": fact,
            "type": "aggregate",
            "scope": {},
            "time_via": time_via,
            "unit": _normalize_unit(rule.get("unit"), agg),
            "supported_grain": supported,
            "additive": agg in {"sum", "count"},
            "confidence": 0.8,
        }
        semantic.setdefault("metrics", {})[metric_id] = metric
        action = "created"
    metric.update({
        "name_zh": rule["name"],
        "expr": expr,
        "type": "aggregate",
        "fact": fact,
        "scope": scope,
        "time_via": time_via,
        "supported_grain": supported,
        "additive": agg in {"sum", "count"},
        "unit": _normalize_unit(rule.get("unit"), agg) or metric.get("unit"),
        "definition": rule.get("definition"),
        "synonyms": list(dict.fromkeys([
            rule["name"], *rule.get("aliases", []), *metric.get("synonyms", []),
        ])),
        "provenance": "documentation",
        "doc_source": rule.get("source"),
    })
    return metric_id, action


def _find_columns_for_tokens(tokens: list[str], index: CatalogIndex,
                             table: str | None) -> list[tuple[str, str, str]]:
    out = []
    for token in tokens:
        hit = index.find_column(table, token)
        if hit:
            out.append((hit[0], hit[1], token))
    return out


def _apply_formula_rule(rule: dict[str, Any], semantic: dict[str, Any],
                        index: CatalogIndex, ctx: DatasetContext,
                        warnings: list[str]) -> tuple[str | None, str]:
    formula = rule.get("formula") or ""
    match = FORMULA_RE.search(formula)
    if not match:
        return None, "无法解析公式"
    left, op, right = match.group(1), match.group(2), match.group(3)
    cols = _find_columns_for_tokens([left, right], index, rule.get("table_id"))
    numeric_right = bool(re.fullmatch(r"-?\d+(?:\.\d+)?", right))
    if not cols or (len(cols) < 2 and not numeric_right):
        return None, "公式列无法定位"
    fact = cols[0][0]
    if len(cols) > 1 and cols[1][0] != fact:
        return None, "公式列不在同一张表"
    left_ref = f"{fact}.{cols[0][1]}"
    right_ref = (
        str(right) if re.fullmatch(r"-?\d+(?:\.\d+)?", right)
        else f"{fact}.{cols[1][1]}"
    )
    condition = f"{left_ref} {op} {right_ref}"
    scope = _map_scope(rule, semantic, index, fact, ctx, warnings)
    time_via = _time_via(semantic, fact)
    supported = _supported_grain(
        semantic, fact, include_reachable=rule.get("aggregation") == "ratio"
    )
    grain = ((semantic.get("tables") or {}).get(fact) or {}).get("grain") or []

    if rule.get("aggregation") == "ratio":
        numerator = f"COUNT(CASE WHEN {condition} THEN 1 END)"
        denominator = (
            f"COUNT(DISTINCT {fact}.{grain[0]})" if len(grain) == 1 else "COUNT(*)"
        )
        existing = next(
            (
                (mid, metric) for mid, metric in (semantic.get("metrics") or {}).items()
                if metric.get("type") == "ratio"
                and metric.get("numerator") == numerator
                and metric.get("denominator") == denominator
            ),
            None,
        )
        metric_id = existing[0] if existing else _new_metric_id(semantic, rule["name"])
        action = "updated" if existing else "created"
        metric = existing[1] if existing else {}
        metric.update({
            "name_zh": rule["name"],
            "type": "ratio",
            "fact": fact,
            "ratio_grain": "fact",
            "ratio_key": f"{fact}.{grain[0]}",
            "numerator": numerator,
            "denominator": denominator,
            "grain_numerator": f"MAX(CASE WHEN {condition} THEN 1 ELSE 0 END)",
            "grain_denominator": "1",
            "numerator_label": rule["name"],
            "denominator_label": "总数",
            "scope": scope,
            "time_via": time_via,
            "supported_grain": supported,
            "unit": "%",
            "definition": rule.get("definition"),
            "synonyms": list(dict.fromkeys([rule["name"], *rule.get("aliases", [])])),
            "provenance": "documentation",
            "doc_source": rule.get("source"),
        })
        semantic.setdefault("metrics", {})[metric_id] = metric
        return metric_id, action

    expr = f"COUNT(CASE WHEN {condition} THEN 1 END)"
    existing = next(
        (
            (mid, metric) for mid, metric in (semantic.get("metrics") or {}).items()
            if metric.get("expr") == expr and metric.get("type") == "aggregate"
        ),
        None,
    )
    if existing:
        metric_id, metric = existing
        action = "updated"
    else:
        metric_id = _new_metric_id(semantic, rule["name"])
        metric = {
            "fact": fact,
            "type": "aggregate",
            "supported_grain": supported,
            "scope": {},
            "time_via": time_via,
            "additive": True,
            "confidence": 0.8,
        }
        semantic.setdefault("metrics", {})[metric_id] = metric
        action = "created"
    metric.update({
        "name_zh": rule["name"],
        "expr": expr,
        "type": "aggregate",
        "fact": fact,
        "scope": scope,
        "time_via": time_via,
        "supported_grain": supported,
        "additive": True,
        "unit": _normalize_unit(rule.get("unit")) or "条",
        "definition": rule.get("definition"),
        "synonyms": list(dict.fromkeys([
            rule["name"], *rule.get("aliases", []), *metric.get("synonyms", []),
        ])),
        "provenance": "documentation",
        "doc_source": rule.get("source"),
    })
    return metric_id, action


def _short_label(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text.rsplit("/", 1)[-1] if "/" in text else text


def _find_table_by_short_label(index: CatalogIndex, names: set[str]) -> str | None:
    for label, tid in index.table_by_label.items():
        if _short_label(label) in names:
            return tid
    return None


def _entity_column(rule: dict[str, Any], index: CatalogIndex) -> tuple[str, str] | None:
    """Find the natural key the rule talks about, without hard-coded names."""
    resolved = rule.get("resolved_columns") or []
    hits = [(c["table"], c["column"]) for c in resolved if c.get("table") and c.get("column")]
    for item in resolved:
        if not item.get("table") or not item.get("column"):
            continue
        label = str(item.get("label") or item.get("column")).lower()
        if "unique" in label or "natural" in label:
            return item["table"], item["column"]
    if hits:
        return hits[0]
    for label, cols in index.columns_by_label.items():
        if "unique_id" in label or "natural_key" in label:
            for table, column in cols:
                return table, column
    return None


def _apply_entity_rule(rule: dict[str, Any], semantic: dict[str, Any],
                       index: CatalogIndex, ctx: DatasetContext,
                       warnings: list[str]) -> tuple[str | None, str]:
    entity = _entity_column(rule, index)
    if not entity:
        return None, "找不到实体自然键列"
    entity_tid, entity_col = entity

    # Pick the fact that owns the event/order grain and can reach the entity.
    candidates: list[tuple[float, str]] = []
    for tid, rows in index.rows.items():
        if tid == entity_tid:
            continue
        grain = ((semantic.get("tables") or {}).get(tid) or {}).get("grain") or []
        if len(grain) != 1:
            continue
        joined = _has_path(semantic, tid, entity_tid)
        short = _short_label(index.label_by_table.get(tid, tid))
        score = math.log2(max(1, rows))
        score += 3.0 if short in {"orders", "order", "transactions", "transaction",
                                  "fact", "events", "event"} else 0.0
        score += 1.0 if short.startswith("order") else 0.0
        score += 1.0 if (semantic.get("tables", {}).get(tid, {}) or {}).get("time") else 0.0
        score += 2.0 if joined else 0.0
        candidates.append((score, tid))
    if not candidates:
        return None, "找不到可用的订单/事实表"
    candidates.sort(reverse=True)

    fact = None
    for _score, tid in candidates[:4]:
        if _add_join_if_possible(semantic, index, tid, entity_tid, ctx):
            fact = tid
            break
    if not fact:
        fact = _find_table_by_short_label(index, {"orders", "order", "transactions"})
        if not fact:
            return None, "事实表无法关联到实体表"

    grain = ((semantic.get("tables") or {}).get(fact) or {}).get("grain") or []
    count_orders = (
        f"COUNT(DISTINCT {fact}.{grain[0]})" if len(grain) == 1 else "COUNT(*)"
    )
    match = re.search(r"(?:至少|不少于|不低于)\s*(\d+)\s*笔",
                      str(rule.get("definition") or ""))
    min_orders = int(match.group(1)) if match else 2
    scope = _map_scope(rule, semantic, index, fact, ctx, warnings)
    time_via = _time_via(semantic, fact)
    name = rule["name"]
    metric_id = _new_metric_id(semantic, name)
    semantic.setdefault("metrics", {})[metric_id] = {
        "name_zh": name,
        "type": "entity_count",
        "fact": fact,
        "entity": f"{entity_tid}.{entity_col}",
        "entity_join": f"{fact} -> {entity_tid}",
        "count_orders": count_orders,
        "min_orders": min_orders,
        "scope": scope,
        "time_via": time_via,
        "supported_grain": ["none"],
        "unit": _normalize_unit(rule.get("unit")) or ("人" if "客户" in name else "个"),
        "additive": False,
        "definition": rule.get("definition"),
        "synonyms": list(dict.fromkeys([name, *rule.get("aliases", [])])),
        "provenance": "documentation",
        "doc_source": rule.get("source"),
    }
    return metric_id, "created"


def apply_documentation(ctx: DatasetContext, documentation: dict[str, Any]) -> dict[str, Any]:
    semantic_path = ctx.semantic_path
    draft_path = ctx.root / "semantic_draft.json"
    semantic = _read_yaml(semantic_path)
    original = yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False)
    index = CatalogIndex.build(ctx)

    applied: list[dict[str, Any]] = []
    warnings: list[str] = list(documentation.get("warnings") or [])
    for rule in documentation.get("rules") or []:
        try:
            if re.search(r"复购|回头客", str(rule.get("name") or "")) or re.search(
                r"至少\s*2\s*笔|不少于\s*2\s*笔", str(rule.get("definition") or "")
            ):
                metric_id, action = _apply_entity_rule(
                    rule, semantic, index, ctx, warnings
                )
            elif (
                rule.get("formula")
                and FORMULA_RE.search(str(rule.get("formula") or ""))
                and rule.get("aggregation") in {None, "count", "ratio"}
            ):
                metric_id, action = _apply_formula_rule(
                    rule, semantic, index, ctx, warnings
                )
            elif rule.get("aggregation"):
                metric_id, action = _apply_aggregate_rule(
                    rule, semantic, index, ctx, warnings
                )
            else:
                metric_id, action = None, f"缺少聚合方式（{rule['name']}）"
        except Exception as exc:
            metric_id, action = None, f"{type(exc).__name__}: {exc}"
        if metric_id:
            applied.append({
                "metric_id": metric_id,
                "name": rule["name"],
                "action": action,
            })
        else:
            warnings.append(f"{rule.get('name')}: {action}")

    semantic["status"] = "draft"
    semantic["documentation"] = {
        "source": documentation.get("source"),
        "parsed_at": documentation.get("parsed_at"),
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "rules": len(applied),
    }
    history = ctx.root / "history"
    history.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(semantic_path, history / f"semantic-before-doc-{stamp}.yaml")
    _write_atomic(semantic_path, yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False))

    try:
        loaded = load_semantic(semantic_path, ctx.binding_path,
                               catalog_path=ctx.catalog_path)
        probe_id = applied[0]["metric_id"] if applied else next(iter(loaded.metrics), None)
        if probe_id:
            from app.domain.pipeline import execute_prepared, prepare
            from app.domain.plan import QueryPlan
            plan = QueryPlan(metric=probe_id, dimensions=[], limit=5)
            compiled, _ = prepare(plan, loaded)
            execute_prepared(compiled, db_path=ctx.warehouse)
    except Exception as exc:
        _write_atomic(semantic_path, original)
        raise DocumentationError(f"口径应用校验失败，已回滚: {type(exc).__name__}: {exc}") from exc

    descriptor_path = ctx.root / "dataset.json"
    descriptor = {}
    if descriptor_path.exists():
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            descriptor = {}
    descriptor.update({
        "status": "draft",
        "documentation": documentation.get("source"),
        "documentation_applied_at": semantic["documentation"]["applied_at"],
    })
    _write_atomic(descriptor_path, json.dumps(descriptor, ensure_ascii=False, indent=2))

    if draft_path.exists():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            applied_ids = {item["metric_id"] for item in applied}
            for metric_id, metric in (semantic.get("metrics") or {}).items():
                if metric_id not in applied_ids:
                    continue
                rows = [m for m in draft.get("metrics", []) if m.get("id") == metric_id]
                if rows:
                    rows[0].update({
                        "name_zh": metric["name_zh"],
                        "name": metric["name_zh"],
                        "selected": True,
                        "provenance": "documentation",
                        "definition": metric.get("definition"),
                    })
                else:
                    draft.setdefault("metrics", []).append({
                        "id": metric_id,
                        "name_zh": metric["name_zh"],
                        "name": metric["name_zh"],
                        "aggregation": metric.get("type", "aggregate").upper(),
                        "fact": metric.get("fact"),
                        "expr": metric.get("expr"),
                        "selected": True,
                        "provenance": "documentation",
                        "definition": metric.get("definition"),
                    })
            draft["documentation"] = semantic["documentation"]
            draft["status"] = "draft"
            _write_atomic(draft_path, json.dumps(draft, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            pass

    # Documentation may create a second metric with an existing display name;
    # keep one selected default and let the UI show the other as a candidate.
    try:
        from app.ingest.semantic_draft import dedupe_selected
        draft_doc = None
        if draft_path.exists():
            draft_doc = json.loads(draft_path.read_text(encoding="utf-8"))
        dedupe_selected(semantic, draft_doc)
        _write_atomic(
            semantic_path,
            yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False),
        )
        if draft_doc is not None:
            _write_atomic(
                draft_path,
                json.dumps(draft_doc, ensure_ascii=False, indent=2),
            )
    except Exception as exc:
        warnings.append(f"指标去重未完成: {type(exc).__name__}: {exc}")

    return {
        "dataset_id": ctx.dataset_id,
        "status": "draft",
        "applied": applied,
        "warnings": warnings,
        "semantic_version": semantic.get("version", 1),
    }
