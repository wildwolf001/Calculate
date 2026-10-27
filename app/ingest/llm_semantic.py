"""LLM-assisted semantic drafting.

The model receives a compact physical profile (tables, columns, values,
detected joins) and returns *structured* metric definitions. It never writes
SQL: expressions are built here from an allowlist, every candidate is compiled
and executed against the warehouse, and the result stays a draft for human
review.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable

import duckdb

from app.config.settings import (
    LLM_ENABLED,
    LLM_SEMANTIC_DRAFT,
    LLM_SEMANTIC_TIMEOUT_SECONDS,
)
from app.core.logging import log_event, logger
from app.domain.pipeline import prepare
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.ingest.semantic_draft import dedupe_selected, _short_label
from app.llm.client import LLMClient

Progress = Callable[[str, int, str], None]
MAX_METRICS = 18
MAX_PROBES = 60

SYSTEM_PROMPT = """你是资深数据建模师。根据给定的物理表、列、关系、维度和样例值，生成可审核的业务指标草稿。
只输出一个 JSON 对象，不要解释、不要 Markdown、不要写 SQL。
硬性要求：
1. fact / table / column 必须来自给定内容；取值必须来自给定样例；不得编造。
2. aggregation 只能是 sum / avg / count / count_distinct / ratio / entity_count。
3. 不要生成“记录数”这类无业务含义的指标；优先生成真正会被业务提问的指标。
4. 业务范围（如只算已送达）只能用真实存在的列和取值表达；没有依据就不要加。
5. dimensions 只能从给定的维度 id/名称中选择；不确定就留空。
6. 最多 15 个指标。宁缺毋滥，拿不准的放 notes。
输出结构（表/列请换成上面 schema 里的真实 id/名称）：
{"metrics":[
  {"name":"商品销售额","synonyms":["销售额","GMV"],"fact":"<含price的事实表id>",
   "aggregation":"sum","column":"price","filters":[{"table":"<订单表id>","column":"order_status","value":"delivered"}],
   "unit":"元","definition":"...","dimensions":["d_..."]},
  {"name":"延迟率","fact":"<订单表id>","aggregation":"ratio",
   "numerator_condition":{"table":"<订单表id>","left":"delivered_at","op":">","right":"estimated_delivery_at"},
   "denominator":{"kind":"count_distinct","table":"<订单表id>","column":"order_id"},
   "filters":[{"table":"<订单表id>","column":"order_status","value":"delivered"}],"unit":"%"},
  {"name":"复购客户","fact":"<订单表id>","aggregation":"entity_count",
   "entity":{"table":"<客户表id>","column":"customer_unique_id"},"min_orders":2,
   "filters":[{"table":"<订单表id>","column":"order_status","value":"delivered"}],"unit":"人"}
],"notes":[]}"""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _slug(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value)).strip("_")
    return (text or "metric").lower()


def _reach_tables(semantic: dict[str, Any], start: str) -> set[str]:
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


def _n1_tables(semantic: dict[str, Any], start: str) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        for parent in (semantic.get("joins") or {}).get(node, {}):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _time_via(semantic: dict[str, Any], fact: str) -> str | None:
    own = ((semantic.get("tables") or {}).get(fact) or {}).get("time")
    if own:
        return f"{fact}.{own}"
    for parent in (semantic.get("joins") or {}).get(fact, {}):
        parent_time = ((semantic.get("tables") or {}).get(parent) or {}).get("time")
        if parent_time:
            return f"{parent}.{parent_time}"
    return None


def _dim_id(semantic: dict[str, Any], value: str) -> str | None:
    if value in (semantic.get("dimensions") or {}):
        return value
    target = str(value or "").strip().lower()
    if not target:
        return None
    for dim_id, dim in (semantic.get("dimensions") or {}).items():
        names = [dim_id, dim.get("name_zh", ""), *(dim.get("synonyms") or [])]
        if target in {str(n).strip().lower() for n in names if n}:
            return dim_id
    return None


def _safe_dims(semantic: dict[str, Any], fact: str, include_reachable: bool) -> list[str]:
    allowed = _reach_tables(semantic, fact) if include_reachable else _n1_tables(semantic, fact)
    dims = ["none"]
    if _time_via(semantic, fact):
        dims.append("month")
    for dim_id, dim in (semantic.get("dimensions") or {}).items():
        kind = dim.get("kind", "categorical")
        if kind == "time":
            continue
        if kind == "derived":
            table = str(dim.get("requires") or "")
        else:
            column = str(dim.get("column") or "")
            table = column.split(".", 1)[0] if "." in column else ""
        if table and table in allowed:
            dims.append(dim_id)
    return list(dict.fromkeys(dims))


def _layer(semantic: dict[str, Any], binding: dict[str, Any]) -> SemanticLayer:
    return SemanticLayer(
        version=int(semantic.get("version", 1)),
        dataset=str(semantic.get("dataset") or ""),
        tables=semantic.get("tables") or {},
        joins=semantic.get("joins") or {},
        pre_aggregates=semantic.get("pre_aggregates") or {},
        metrics={
            mid: Metric(mid, raw)
            for mid, raw in (semantic.get("metrics") or {}).items()
        },
        dimensions={
            did: Dimension(did, raw)
            for did, raw in (semantic.get("dimensions") or {}).items()
        },
        policies=semantic.get("policies") or {},
        time_range=tuple(semantic.get("time_range") or ["", ""]),
        binding=binding,
        capabilities=semantic.get("capabilities") or {},
        parser="generic",
        raw=semantic,
    )


# ---------------------------------------------------------------------------
# profile + prompt
# ---------------------------------------------------------------------------

def build_profile(semantic: dict[str, Any], tables: list[Any],
                  dim_values: dict[str, Any], max_chars: int = 14000) -> str:
    by_id = {t.table_id: t for t in tables}
    lines: list[str] = ["表结构（表id \"名称\" 行数 粒度 时间列）:"]
    for tid, spec in (semantic.get("tables") or {}).items():
        table = by_id.get(tid)
        if table is None:
            continue
        lines.append(
            f"- {tid} \"{_short_label(table.label)}\" rows={table.rows} "
            f"grain={','.join(spec.get('grain') or [])} time={spec.get('time') or '-'}"
        )
        for col in table.columns[:40]:
            samples = ", ".join(str(s) for s in (col.samples or [])[:5])
            lines.append(
                f"  {col.physical}({col.label}) {col.inferred_type} "
                f"distinct={col.distinct} 例:{samples}"
            )
    lines.append("\n已推断关系（子表.列 -> 父表.列，N:1）:")
    for child, links in (semantic.get("joins") or {}).items():
        for parent, spec in links.items():
            lines.append(
                f"- {child}.{spec.get('left')} -> {parent}.{spec.get('right')}"
            )
    lines.append("\n可用维度（id \"名称\" 列 样例）:")
    values = (dim_values or {}).get("values") or {}
    for dim_id, dim in (semantic.get("dimensions") or {}).items():
        if dim.get("kind") == "time":
            continue
        column = str(dim.get("column") or "")
        samples = values.get(column) or values.get(str(dim.get("filter_lookup") or "")) or []
        sample_text = "、".join(str(v) for v in samples[:12])
        lines.append(f"- {dim_id} \"{dim.get('name_zh', dim_id)}\" {column} 例:{sample_text}")
    lines.append("\n已有指标（名称 / 类型）:")
    for raw in (semantic.get("metrics") or {}).values():
        lines.append(f"- {raw.get('name_zh')} / {raw.get('type', 'aggregate')}")
    text = "\n".join(lines)
    return text[:max_chars]


def _user_prompt(profile: str) -> str:
    return (
        "请基于下面的 schema 生成业务指标草稿。只输出 JSON。\n\n"
        + profile
        + "\n\n输出：{\"metrics\":[...],\"notes\":[]}"
    )


# ---------------------------------------------------------------------------
# resolve + build + probe
# ---------------------------------------------------------------------------

def _table_map(tables: list[Any]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    labels: dict[str, str] = {}
    columns: dict[str, dict[str, str]] = {}
    for table in tables:
        labels[table.table_id.lower()] = table.table_id
        labels[_short_label(table.label).lower()] = table.table_id
        cols: dict[str, str] = {}
        for col in table.columns:
            cols[str(col.label).lower()] = col.physical
            cols[str(col.physical).lower()] = col.physical
        columns[table.table_id] = cols
    return labels, columns


def _resolve_table(value: Any, labels: dict[str, str], semantic: dict[str, Any]) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in (semantic.get("tables") or {}):
        return text
    for key in (text.lower(), _short_label(text).lower()):
        if key in labels:
            return labels[key]
    return None


def _resolve_column(table: str, value: Any,
                    columns: dict[str, dict[str, str]]) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "." in text:
        maybe_table, _, maybe_col = text.partition(".")
        if maybe_table in columns:
            table, text = maybe_table, maybe_col
    return columns.get(table, {}).get(text.lower())


def _actual_value(con: duckdb.DuckDBPyConnection, table: str, column: str,
                  value: Any) -> Any:
    row = con.execute(
        f"SELECT {_q(column)} FROM {_q(table)} "
        f"WHERE {_q(column)} IS NOT NULL "
        f"AND lower(CAST({_q(column)} AS VARCHAR)) = lower(?) LIMIT 1",
        [str(value)],
    ).fetchone()
    if not row:
        raise ValueError(f"筛选值不存在: {table}.{column}={value}")
    return row[0]


def _scope_from_filters(filters: Any, fact: str, semantic: dict[str, Any],
                        labels: dict[str, str],
                        columns: dict[str, dict[str, str]],
                        con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    if isinstance(filters, dict):
        filters = [
            {"column": key, "value": value} for key, value in filters.items()
        ]
    if not isinstance(filters, list):
        return scope
    reachable = _reach_tables(semantic, fact)
    for item in filters:
        if not isinstance(item, dict):
            continue
        raw_table = item.get("table")
        table = _resolve_table(raw_table, labels, semantic) if raw_table else fact
        if not table:
            raise ValueError(f"筛选表不存在: {raw_table}")
        column = _resolve_column(table, item.get("column"), columns)
        if not column:
            # Forgiving path: locate a uniquely named column on a reachable table.
            name = str(item.get("column") or "").lower()
            hits = [
                (tid, cols[name]) for tid, cols in columns.items()
                if tid in reachable and name in cols
            ]
            if len(hits) == 1:
                table, column = hits[0]
        if not column:
            raise ValueError(f"筛选列不存在: {item}")
        if table not in reachable:
            raise ValueError(f"筛选表 {table} 无法从事实 {fact} 关联")
        scope[f"{table}.{column}"] = _actual_value(con, table, column, item.get("value"))
    return scope


def _condition_text(cond: Any, fact: str, labels: dict[str, str],
                    columns: dict[str, dict[str, str]]) -> str | None:
    if not isinstance(cond, dict):
        return None
    table = str(cond.get("table") or fact)
    table = labels.get(table.lower(), table)
    left = _resolve_column(table, cond.get("left") or cond.get("column"), columns)
    op = str(cond.get("op") or ">")
    if op not in {">", ">=", "<", "<=", "=", "<>", "!="}:
        raise ValueError(f"不支持的比较符: {op}")
    if not left:
        raise ValueError(f"条件左列不存在: {cond}")
    right = cond.get("right")
    if isinstance(right, (int, float)) and not isinstance(right, bool):
        right_ref = str(right)
    else:
        right_col = _resolve_column(table, right, columns)
        if not right_col:
            raise ValueError(f"条件右列不存在: {cond}")
        right_ref = f"{table}.{right_col}"
    op = "<>" if op == "!=" else op
    return f"{table}.{left} {op} {right_ref}"


def _requested_dims(spec: dict[str, Any], semantic: dict[str, Any]) -> list[str] | None:
    raw = spec.get("dimensions") or spec.get("supported_dimensions")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        dim_id = _dim_id(semantic, str(item))
        if dim_id and dim_id not in out:
            out.append(dim_id)
    return out or None


def _probe_metric(semantic: dict[str, Any], binding: dict[str, Any],
                  metric_id: str, dims: list[str],
                  con: duckdb.DuckDBPyConnection) -> bool:
    try:
        layer = _layer(semantic, binding)
        plan = QueryPlan(metric=metric_id, dimensions=dims, limit=5)
        compiled, _ = prepare(plan, layer)
        con.execute(compiled.sql, compiled.params)
        return True
    except Exception:
        return False


def _build_metric(spec: dict[str, Any], semantic: dict[str, Any],
                  binding: dict[str, Any], tables: list[Any],
                  labels: dict[str, str], columns: dict[str, dict[str, str]],
                  con: duckdb.DuckDBPyConnection,
                  probe_state: dict[str, int]) -> tuple[str, dict[str, Any], list[str]]:
    warnings: list[str] = []
    name = str(spec.get("name") or "").strip()
    if not name:
        raise ValueError("缺少指标名")
    fact = _resolve_table(spec.get("fact"), labels, semantic)
    if not fact or fact not in (semantic.get("tables") or {}):
        raise ValueError(f"未知事实表: {spec.get('fact')}")
    agg = str(spec.get("aggregation") or "").lower()
    if agg not in {"sum", "avg", "count", "count_distinct", "ratio", "entity_count"}:
        raise ValueError(f"不支持的聚合: {agg}")

    scope = _scope_from_filters(
        spec.get("filters"), fact, semantic, labels, columns, con
    )
    time_via = _time_via(semantic, fact)
    requested = _requested_dims(spec, semantic)
    definition = str(spec.get("definition") or "")
    synonyms = [str(x) for x in (spec.get("synonyms") or []) if x][:10]
    unit = spec.get("unit")

    if agg == "entity_count":
        entity = spec.get("entity") or {}
        entity_table = _resolve_table(entity.get("table"), labels, semantic)
        if not entity_table:
            raise ValueError("entity_count 缺少实体表")
        entity_col = _resolve_column(entity_table, entity.get("column"), columns)
        if not entity_col:
            raise ValueError("entity_count 缺少实体列")
        if entity_table not in _reach_tables(semantic, fact):
            raise ValueError("实体表无法从事实表关联")
        grain = ((semantic.get("tables") or {}).get(fact) or {}).get("grain") or []
        if len(grain) != 1:
            raise ValueError("entity_count 的事实表需要唯一主键")
        metric = {
            "name_zh": name,
            "type": "entity_count",
            "fact": fact,
            "entity": f"{entity_table}.{entity_col}",
            "entity_join": f"{fact} -> {entity_table}",
            "count_orders": f"COUNT(DISTINCT {fact}.{grain[0]})",
            "min_orders": int(spec.get("min_orders") or 2),
            "scope": scope,
            "time_via": time_via,
            "supported_grain": ["none"],
            "unit": unit or ("人" if "客户" in name else "个"),
            "additive": False,
            "definition": definition,
            "synonyms": list(dict.fromkeys([name, *synonyms])),
            "provenance": "llm",
            "confidence": 0.7,
        }
        return _accept(metric, semantic, binding, con, probe_state, warnings)

    if agg == "ratio":
        grain = ((semantic.get("tables") or {}).get(fact) or {}).get("grain") or []
        if len(grain) != 1:
            raise ValueError("ratio 的事实表需要唯一主键")
        condition = _condition_text(
            spec.get("numerator_condition") or spec.get("condition"),
            fact, labels, columns,
        )
        if not condition:
            raise ValueError("ratio 缺少分子条件")
        den = spec.get("denominator") or {}
        if isinstance(den, dict) and den.get("kind") == "count_distinct":
            den_table = _resolve_table(den.get("table") or fact, labels, semantic) or fact
            den_col = _resolve_column(den_table, den.get("column"), columns)
            if not den_col:
                raise ValueError("ratio 分母列不存在")
            denominator = f"COUNT(DISTINCT {den_table}.{den_col})"
        else:
            denominator = "COUNT(*)"
        supported = requested if requested is not None else _safe_dims(semantic, fact, True)
        if "none" not in supported:
            supported = ["none", *supported]
        metric = {
            "name_zh": name,
            "type": "ratio",
            "fact": fact,
            "ratio_grain": "fact",
            "ratio_key": f"{fact}.{grain[0]}",
            "numerator": f"COUNT(CASE WHEN {condition} THEN 1 END)",
            "denominator": denominator,
            "grain_numerator": f"MAX(CASE WHEN {condition} THEN 1 ELSE 0 END)",
            "grain_denominator": "1",
            "numerator_label": name,
            "denominator_label": "总数",
            "scope": scope,
            "time_via": time_via,
            "supported_grain": supported,
            "unit": unit or "%",
            "additive": False,
            "definition": definition,
            "synonyms": list(dict.fromkeys([name, *synonyms])),
            "provenance": "llm",
            "confidence": 0.7,
        }
        return _accept(metric, semantic, binding, con, probe_state, warnings)

    column = _resolve_column(fact, spec.get("column"), columns)
    column_table = fact
    if column is None and spec.get("column"):
        # Allow fully qualified table.column.
        text = str(spec.get("column"))
        if "." in text:
            maybe_table = _resolve_table(text.partition(".")[0], labels, semantic)
            if maybe_table:
                column_table = maybe_table
                column = _resolve_column(maybe_table, text.partition(".")[2], columns)
    if agg in {"sum", "avg", "count_distinct", "count"} and not column:
        raise ValueError(f"{agg} 缺少列")
    allowed_tables = (
        _reach_tables(semantic, fact)
        if agg == "count_distinct"
        else _n1_tables(semantic, fact)
    )
    if column_table not in allowed_tables:
        raise ValueError(f"列 {column_table} 会造成连接放大，不能用于 {agg}")

    if agg == "sum":
        expr = f"SUM({column_table}.{column})"
    elif agg == "avg":
        expr = f"AVG({column_table}.{column})"
    elif agg == "count_distinct":
        expr = f"COUNT(DISTINCT {column_table}.{column})"
    else:
        condition_spec = spec.get("condition")
        condition = _condition_text(condition_spec, fact, labels, columns)
        if condition:
            cond_table = fact
            if isinstance(condition_spec, dict) and condition_spec.get("table"):
                cond_table = _resolve_table(
                    condition_spec.get("table"), labels, semantic
                ) or fact
            if cond_table != fact or column_table != fact:
                raise ValueError("带条件的计数必须在事实表上")
            expr = f"COUNT(CASE WHEN {condition} THEN 1 END)"
        else:
            expr = f"COUNT({column_table}.{column})"
    supported = requested if requested is not None else _safe_dims(
        semantic, fact, agg == "count_distinct"
    )
    if "none" not in supported:
        supported = ["none", *supported]
    metric = {
        "name_zh": name,
        "type": "aggregate",
        "fact": fact,
        "expr": expr,
        "scope": scope,
        "time_via": time_via,
        "supported_grain": supported,
        "unit": unit,
        "additive": agg in {"sum", "count"},
        "definition": definition,
        "synonyms": list(dict.fromkeys([name, *synonyms])),
        "provenance": "llm",
        "confidence": 0.7,
    }
    return _accept(metric, semantic, binding, con, probe_state, warnings)


def _accept(metric: dict[str, Any], semantic: dict[str, Any],
            binding: dict[str, Any], con: duckdb.DuckDBPyConnection,
            probe_state: dict[str, int],
            warnings: list[str]) -> tuple[str, dict[str, Any], list[str]]:
    name = metric["name_zh"]
    existing = next(
        (mid for mid, raw in (semantic.get("metrics") or {}).items()
         if raw.get("name_zh") == name),
        None,
    )
    metric_id = existing or f"m_llm_{_slug(name)}"
    while not existing and metric_id in (semantic.get("metrics") or {}):
        metric_id = f"{metric_id}_x"

    candidate = copy.deepcopy(semantic)
    candidate.setdefault("metrics", {})[metric_id] = metric
    if probe_state["count"] >= MAX_PROBES:
        raise ValueError("探针数量超过上限")
    probe_state["count"] += 1
    if not _probe_metric(candidate, binding, metric_id, [], con):
        raise ValueError("指标探针执行失败")
    dims = [d for d in metric.get("supported_grain") or [] if d not in {"none", "month"}]
    for dim in dims[:3]:
        if probe_state["count"] >= MAX_PROBES:
            break
        probe_state["count"] += 1
        if not _probe_metric(candidate, binding, metric_id, [dim], con):
            warnings.append(f"{name}: 维度 {dim} 无法执行，已移除")
            metric["supported_grain"] = [
                d for d in metric["supported_grain"] if d != dim
            ]
            candidate["metrics"][metric_id] = metric
    if "month" in (metric.get("supported_grain") or []):
        if probe_state["count"] < MAX_PROBES:
            probe_state["count"] += 1
            if not _probe_metric(candidate, binding, metric_id, ["month"], con):
                warnings.append(f"{name}: 月份维度无法执行，已移除")
                metric["supported_grain"] = [
                    d for d in metric["supported_grain"] if d != "month"
                ]
    return metric_id, metric, warnings


# ---------------------------------------------------------------------------
# public entrypoint
# ---------------------------------------------------------------------------

def refine_semantic(semantic: dict[str, Any], draft: dict[str, Any],
                    binding: dict[str, Any], tables: list[Any],
                    dim_values: dict[str, Any],
                    con: duckdb.DuckDBPyConnection,
                    progress: Progress | None = None) -> dict[str, Any]:
    if not (LLM_ENABLED and LLM_SEMANTIC_DRAFT):
        return {"metrics": [], "warnings": []}
    client = LLMClient(timeout=LLM_SEMANTIC_TIMEOUT_SECONDS)
    if not client.available:
        return {"metrics": [], "warnings": []}

    if progress is not None:
        progress("semantic", 78, "模型生成口径草稿")
    profile = build_profile(semantic, tables, dim_values)
    try:
        data = client.complete_json(
            SYSTEM_PROMPT, _user_prompt(profile),
            max_tokens=8000, stage="semantic_draft",
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log_event(logger, "semantic_draft.llm_failed", error=reason[:500])
        return {"metrics": [], "warnings": [f"模型语义草稿失败（{type(exc).__name__}）"]}

    raw_metrics = data.get("metrics") if isinstance(data, dict) else None
    if not isinstance(raw_metrics, list):
        return {"metrics": [], "warnings": ["模型语义草稿格式不正确"]}

    labels, columns = _table_map(tables)
    accepted: list[dict[str, Any]] = []
    warnings: list[str] = list(data.get("notes") or [])[:5]
    probe_state = {"count": 0}
    for index, spec in enumerate(raw_metrics[:MAX_METRICS]):
        if not isinstance(spec, dict):
            continue
        if progress is not None:
            progress(
                "semantic", min(84, 80 + int(4 * index / max(1, len(raw_metrics)))),
                f"校验模型指标 {spec.get('name') or index + 1}",
            )
        try:
            metric_id, metric, notes = _build_metric(
                spec, semantic, binding, tables, labels, columns, con, probe_state
            )
        except Exception as exc:
            warnings.append(f"{spec.get('name') or index + 1}: {exc}")
            continue
        semantic.setdefault("metrics", {})[metric_id] = metric
        accepted.append({"id": metric_id, "name": metric["name_zh"], "type": metric["type"]})
        warnings.extend(notes)

    # A later proposal may override an earlier one with the same name/id.
    accepted = [
        {
            "id": entry["id"],
            "name": semantic["metrics"][entry["id"]].get("name_zh", entry["name"]),
            "type": semantic["metrics"][entry["id"]].get("type", entry["type"]),
        }
        for entry in {e["id"]: e for e in accepted}.values()
        if entry["id"] in (semantic.get("metrics") or {})
    ]
    if not accepted:
        return {"metrics": [], "warnings": warnings}

    dedupe_selected(semantic, draft)
    for entry in accepted:
        metric = semantic["metrics"][entry["id"]]
        item = next(
            (m for m in draft.get("metrics") or [] if m.get("id") == entry["id"]), None
        )
        payload = {
            "id": entry["id"],
            "name_zh": metric["name_zh"],
            "name": metric["name_zh"],
            "aggregation": metric.get("type", "aggregate").upper(),
            "fact": metric.get("fact"),
            "expr": metric.get("expr") or metric.get("numerator"),
            "unit": metric.get("unit"),
            "definition": metric.get("definition"),
            "synonyms": metric.get("synonyms"),
            "confidence": metric.get("confidence", 0.7),
            "selected": bool(metric.get("selected", True)),
            "provenance": "llm",
        }
        if item is None:
            draft.setdefault("metrics", []).append(payload)
        else:
            item.update(payload)
    if draft.get("warnings") is not None:
        draft["warnings"].append(
            f"模型生成了 {len(accepted)} 个指标口径，请在发布前核对；"
            "仅接受通过执行探针的候选。"
        )
    log_event(logger, "semantic_draft.accepted", count=len(accepted),
              warnings=len(warnings), probes=probe_state["count"])
    return {
        "metrics": accepted,
        "warnings": warnings,
        "probes": probe_state["count"],
    }
