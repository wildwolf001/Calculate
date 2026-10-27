"""QueryPlan -> SQL. This is the only place SQL text is assembled.

Rules (ARCHITECTURE.md §6.3):
- metric scope is injected automatically;
- time uses metric.time_via;
- every join is N:1 unless the metric explicitly needs order_items for AOV;
- ratios are computed at the declared grain (order / review);
- payment metrics pre-aggregate by order before joining;
- all values are bound parameters; only validated integers are inlined (LIMIT).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer, SemanticError


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


@dataclass
class Compiled:
    sql: str
    params: list
    columns: list[str]
    applied_time_filter: bool
    tables_used: list[str]
    meta: dict = field(default_factory=dict)

    def fingerprint(self, compiler_version: int = 1, semantic_version: int = 1) -> str:
        payload = json.dumps(
            {"sql": self.sql, "params": [str(p) for p in self.params],
             "compiler": compiler_version, "semantic": semantic_version},
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class _Builder:
    def __init__(self, plan: QueryPlan, sem: SemanticLayer):
        self.plan = plan
        self.sem = sem
        self.metric = sem.metric(plan.metric)
        self.params: list = []
        self.tables_used: set[str] = set()
        self.applied_time = False
        self.notes: list[str] = list(plan.notes)
        if plan.metric == "avg_order_value":
            self.notes.append("客单价 = 商品销售额 / 已送达订单数；按订单粒度计算。")

    # ---------- helpers ----------
    def p(self, value):
        self.params.append(value)
        return "?"

    def phys(self, ref: str) -> str:
        """Resolve `table.logical` to `table."physical"`."""
        table, _, col = ref.partition(".")
        return f"{_q(table)}.{_q(self.sem.column(ref))}" if col else _q(table)

    def _table_of(self, ref: str) -> str:
        return ref.split(".", 1)[0]

    def _expr_tables(self, expr: str) -> set[str]:
        return {t for t in self.sem.tables if f"{t}." in expr}

    def _dim_column(self, dim_id: str) -> str:
        dim = self.sem.dimension(dim_id)
        if dim.kind == "time":
            time_col = self.metric.raw.get("time_via")
            fmt = dim.raw.get("format", "%Y-%m")
            return f"strftime({self.phys(time_col)}, '{fmt}')"
        if dim.kind == "derived":
            labels = dim.raw.get("labels", {})
            t = labels.get("true", "true")
            f = labels.get("false", "false")
            return f"CASE WHEN {dim.raw['expr']} THEN '{t}' ELSE '{f}' END"
        return self.phys(dim.raw["column"])

    def _filter_condition(self, dim_id: str, op: str, value) -> str:
        dim = self.sem.dimension(dim_id)
        if dim.kind == "derived":
            cond = dim.raw["expr"]
            if isinstance(value, str):
                v = value.strip().lower() in {"true", "1", "是", "延迟", "低分"}
            else:
                v = bool(value)
            if not v:
                cond = f"NOT ({cond})"
            return f"({cond})"
        if dim.kind == "time":
            col = self._dim_column(dim_id)
        else:
            col = self.phys(dim.raw.get("filter_column") or dim.raw["column"])

        # Grounding may return a single-ID list while the parser still says
        # "eq"; binding the list as one parameter crashes DuckDB. Normalize
        # list-valued equality to IN/NOT IN before rendering.
        if isinstance(value, (list, tuple, set)):
            vals = list(value)
            if op == "eq":
                op = "in"
            elif op == "neq":
                if not vals:
                    return "TRUE"
                marks = ", ".join(self.p(v) for v in vals)
                return f"{col} NOT IN ({marks})"
            elif op in {"gte", "lte", "gt", "lt", "in"}:
                if len(vals) == 1:
                    value = vals[0]
                elif op != "in":
                    raise SemanticError(f"过滤操作符 {op} 只接受单值")
        if op == "in":
            vals = value if isinstance(value, (list, tuple, set)) else [value]
            vals = list(vals)
            if not vals:
                return "FALSE"
            marks = ", ".join(self.p(v) for v in vals)
            return f"{col} IN ({marks})"
        symbols = {"eq": "=", "neq": "<>", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}
        if op not in symbols:
            raise SemanticError(f"不支持的过滤操作符: {op}")
        return f"{col} {symbols[op]} {self.p(value)}"

    def _needed_tables(self) -> set[str]:
        needed = {self.metric.fact}
        # metric-level references
        for expr_key in ("expr", "numerator", "denominator", "grain_numerator",
                         "grain_denominator", "count_orders", "entity"):
            expr = self.metric.raw.get(expr_key)
            if isinstance(expr, str):
                needed |= self._expr_tables(expr)
        time_via = self.metric.raw.get("time_via")
        if isinstance(time_via, str):
            needed |= self._expr_tables(time_via)
        for ref in self.metric.raw.get("scope", {}):
            needed.add(self._table_of(ref))
        # dimensions & filters
        for dim_id in list(self.plan.dimensions) + list(self.plan.filters):
            dim = self.sem.dimension(dim_id)
            if dim.kind in {"categorical", "time"}:
                for step in dim.join_path:
                    src, _, dst = step.partition(" -> ")
                    needed.add(src)
                    needed.add(dst)
                if dim.raw.get("column"):
                    needed.add(self._table_of(dim.raw["column"]))
            elif dim.kind == "derived":
                needed.add(dim.raw["requires"])
        # order_reviews time_via needs orders (scope) already covered above
        return needed

    def _from_clause(self) -> str:
        fact = self.metric.fact
        self.tables_used.add(fact)
        clause = self._fact_source()
        # BFS over the join graph, starting at fact.
        from collections import deque

        adj: dict[str, list[tuple[str, str, str, str]]] = {}
        for src, targets in self.sem.joins.items():
            for dst, spec in targets.items():
                left = spec.get("left") or spec.get("on")
                right = spec.get("right") or spec.get("on")
                if not left or not right:
                    raise SemanticError(f"join {src}->{dst} 缺少连接列")
                adj.setdefault(src, []).append((dst, left, right, "forward"))
                adj.setdefault(dst, []).append((src, right, left, "reverse"))

        needed = self._needed_tables()
        parent: dict[str, tuple[str, str, str, str, int] | None] = {fact: None}
        depth: dict[str, int] = {fact: 0}
        seen = {fact}
        queue = deque([fact])
        while queue:
            node = queue.popleft()
            for dst, node_col, dst_col, direction in adj.get(node, []):
                if dst in seen:
                    continue
                seen.add(dst)
                parent[dst] = (node, node_col, dst_col, direction, depth[node])
                depth[dst] = depth[node] + 1
                queue.append(dst)

        # A needed table may be several hops away. Walk its parent chain so
        # intermediate tables are joined too (e.g. order_items -> products ->
        # categories for COUNT(DISTINCT categories.id)).
        edges: dict[frozenset, tuple[int, str]] = {}
        for target in needed:
            if target == fact:
                continue
            if target not in parent:
                raise SemanticError(
                    f"指标 {self.metric.id} 需要的表 {target} 无法从事实表 {fact} 关联"
                )
            cur = target
            while parent[cur] is not None:
                node, node_col, dst_col, direction, node_depth = parent[cur]
                key = frozenset((node, cur))
                if key not in edges:
                    if direction == "forward":
                        cond = f"{_q(node)}.{_q(node_col)} = {_q(cur)}.{_q(dst_col)}"
                    else:
                        cond = f"{_q(cur)}.{_q(dst_col)} = {_q(node)}.{_q(node_col)}"
                    edges[key] = (node_depth, f"JOIN {_q(cur)} ON {cond}")
                cur = node

        for line in [line for _d, line in sorted(edges.values(), key=lambda x: x[0])]:
            clause += "\n  " + line
        self.tables_used |= seen
        return clause

    def _fact_source(self) -> str:
        fact = self.metric.fact
        preagg = self.metric.raw.get("pre_aggregate")
        if not preagg:
            return f"FROM {_q(fact)}"
        spec = self.sem.pre_aggregates.get(preagg)
        if not spec:
            raise SemanticError(f"预聚合不存在: {preagg}")
        group_cols = [spec["key"]]
        # payment_type is the only attribute we allow to survive pre-aggregation
        if "payment_type" in self.plan.dimensions or "payment_type" in self.plan.filters:
            group_cols.append("payment_type")
        measures = ", ".join(f"{expr} AS {_q(alias)}" for alias, expr in spec["measures"].items())
        keys = ", ".join(_q(c) for c in group_cols)
        self.tables_used.add(fact)
        return (
            f"FROM (SELECT {keys}, {measures} FROM {_q(fact)} GROUP BY {keys}) AS {_q(fact)}"
        )

    def _where_clause(self, time_start: date | None = None, time_end: date | None = None) -> str:
        parts: list[str] = []
        for ref, value in self.metric.raw.get("scope", {}).items():
            parts.append(f"{self.phys(ref)} = {self.p(value)}")
        time_via = self.metric.raw.get("time_via")
        if time_via:
            start = time_start or (self.plan.time.start if self.plan.time else None)
            end = time_end or (self.plan.time.end if self.plan.time else None)
            if start and end:
                parts.append(f"{self.phys(time_via)} >= {self.p(start)}")
                parts.append(f"{self.phys(time_via)} < {self.p(end)}")
                self.applied_time = True
        for dim_id, spec in self.plan.filters.items():
            parts.append(self._filter_condition(dim_id, spec.op, spec.value))
        return ("WHERE " + " AND ".join(f"({p})" for p in parts)) if parts else ""

    def _dims_select(self) -> list[str]:
        return [f"{self._dim_column(d)} AS {_q(d)}" for d in self.plan.dimensions]

    def _order_clause(self, allowed: set[str]) -> str:
        parts = []
        for spec in self.plan.order_by:
            field = spec.field
            if field in {self.plan.metric, "value"}:
                field = "value"
            elif field not in allowed:
                raise SemanticError(f"无法按 {field} 排序")
            parts.append(f"{_q(field)} {spec.direction.upper()}")
        # stable tie-breakers: dimensions in declared order
        for d in self.plan.dimensions:
            if not any(s.field == d for s in self.plan.order_by):
                parts.append(f"{_q(d)} ASC")
        return ("ORDER BY " + ", ".join(parts)) if parts else ""

    def _having_clause(self, alias_map: dict[str, str]) -> str:
        parts = []
        symbols = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<", "eq": "="}
        for h in self.plan.having:
            alias = alias_map.get(h.metric, h.metric)
            if alias == "order_count" and "order_count" not in alias_map:
                raise SemanticError("HAVING 需要订单数，但当前计划没有订单数列")
            parts.append(f"{alias} {symbols[h.op]} {self.p(h.value)}")
        return ("HAVING " + " AND ".join(parts)) if parts else ""

    # ---------- compile paths ----------
    def compile(self) -> Compiled:
        if self.plan.comparison:
            return self._compile_explicit_comparison()
        if self.plan.compare:
            return self._compile_compare()
        if self.metric.type == "entity_count":
            return self._compile_entity()
        if self.metric.type == "ratio":
            return self._compile_ratio()
        return self._compile_aggregate()

    def _finalize(self, sql: str, columns: list[str], meta: dict | None = None) -> Compiled:
        return Compiled(
            sql=sql.strip(),
            params=self.params,
            columns=columns,
            applied_time_filter=self.applied_time,
            tables_used=sorted(self.tables_used),
            meta={**(meta or {}), "notes": self.notes},
        )

    def _compile_aggregate(self) -> Compiled:
        if not self.metric.raw.get("expr"):
            raise SemanticError(f"指标 {self.metric.id} 缺少 expr")
        dims = self._dims_select()
        expr = self.metric.raw["expr"]
        select = dims + [f"{expr} AS {_q('value')}"]
        sql = [
            "SELECT " + ", ".join(select),
            self._from_clause(),
            self._where_clause(),
        ]
        if self.plan.dimensions:
            sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(self.plan.dimensions))))
        grain = (self.sem.tables.get(self.metric.fact) or {}).get("grain") or []
        having_count = (
            f"COUNT(DISTINCT {self.phys(f'{self.metric.fact}.{grain[0]}')})"
            if grain else "COUNT(*)"
        )
        sql.append(self._having_clause({"order_count": having_count}))
        order = self._order_clause(set(self.plan.dimensions) | {"value"})
        if order:
            sql.append(order)
        sql.append(f"LIMIT {int(self.plan.limit)}")
        columns = list(self.plan.dimensions) + ["value"]
        return self._finalize("\n".join(x for x in sql if x), columns)

    def _compile_ratio(self) -> Compiled:
        if not self.sem.capabilities.get("ratio", True):
            raise SemanticError("当前数据源未启用比率指标能力")
        grain_key_name = self.plan.ratio_grain or self.metric.raw.get("ratio_grain") or "fact"
        ratio_key = self.metric.raw.get("ratio_key")
        if ratio_key:
            key = self.phys(str(ratio_key))
        else:
            grain = (self.sem.tables.get(self.metric.fact) or {}).get("grain") or []
            if not grain:
                raise SemanticError(
                    f"比率指标 {self.metric.id} 没有可用的分母粒度"
                )
            key = self.phys(f"{self.metric.fact}.{grain[0]}")
        num = self.metric.raw.get("grain_numerator")
        den = self.metric.raw.get("grain_denominator")
        if not num or not den:
            raise SemanticError(f"比率指标 {self.metric.id} 缺少 grain_numerator/denominator")

        dims = self._dims_select()
        inner_select = dims + [f"{key} AS {_q('_key')}", f"{num} AS {_q('_num')}", f"{den} AS {_q('_den')}"]
        group_inner = [str(i + 1) for i in range(len(self.plan.dimensions))] + [str(len(dims) + 1)]
        grain_sql = "\n".join([
            "WITH grain AS (",
            "  SELECT " + ", ".join(inner_select),
            "  " + self._from_clause().replace("\n", "\n  "),
            "  " + self._where_clause().replace("\n", "\n  "),
            "  GROUP BY " + ", ".join(group_inner),
            ")",
        ])
        outer_dims = [f"{_q(d)}" for d in self.plan.dimensions]
        outer_select = outer_dims + [
            f"SUM({_q('_num')}) AS {_q('numerator')}",
            f"SUM({_q('_den')}) AS {_q('denominator')}",
            f"SUM({_q('_num')}) / NULLIF(SUM({_q('_den')}), 0) AS {_q('value')}",
            f"SUM({_q('_den')}) AS {_q('order_count')}",
        ]
        sql = [grain_sql, "SELECT " + ", ".join(outer_select), "FROM grain"]
        if self.plan.dimensions:
            sql.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(self.plan.dimensions))))
        sql.append(self._having_clause({"order_count": _q("order_count"), "value": _q("value")}))
        order = self._order_clause(set(self.plan.dimensions) | {"value", "order_count", "numerator", "denominator"})
        if order:
            sql.append(order)
        sql.append(f"LIMIT {int(self.plan.limit)}")
        columns = list(self.plan.dimensions) + ["numerator", "denominator", "value", "order_count"]
        return self._finalize("\n".join(x for x in sql if x), columns,
                              {"ratio_grain": grain_key_name})

    def _compile_entity(self) -> Compiled:
        if not self.sem.capabilities.get("entity_count", True):
            raise SemanticError("当前数据源未启用实体计数能力")
        entity = self.metric.raw.get("entity")
        count_orders = self.metric.raw.get("count_orders")
        min_orders = int(self.metric.raw.get("min_orders", 2))
        if self.plan.nested and self.plan.nested.min_orders:
            min_orders = int(self.plan.nested.min_orders)
        if not entity or not count_orders:
            raise SemanticError(f"实体指标 {self.metric.id} 定义不完整")
        sql = "\n".join([
            "WITH entity AS (",
            f"  SELECT {self.phys(entity)} AS {_q('_entity')}, {count_orders} AS {_q('_n')}",
            "  " + self._from_clause().replace("\n", "\n  "),
            "  " + self._where_clause().replace("\n", "\n  "),
            "  GROUP BY 1",
            "), filtered AS (",
            f"  SELECT {_q('_entity')}, {_q('_n')} FROM entity WHERE {_q('_n')} >= {self.p(min_orders)}",
            ")",
            f"SELECT COUNT(*) AS {_q('value')} FROM filtered",
            "LIMIT 1",
        ])
        return self._finalize(sql, ["value"], {"min_orders": min_orders, "entity": entity})

    # ---------- compare ----------
    def _period_bounds(self) -> tuple[date, date, date, date]:
        assert self.plan.time
        cur_start, cur_end = self.plan.time.start, self.plan.time.end
        if self.plan.compare and self.plan.compare.type == "prev_year":
            prev_start = date(cur_start.year - 1, cur_start.month, cur_start.day)
            prev_end = date(cur_end.year - 1, cur_end.month, cur_end.day)
            return prev_start, prev_end, cur_start, cur_end
        # prev_period: calendar month when the range is exactly one calendar month
        if cur_start.day == 1 and (cur_end.year, cur_end.month) == (
            cur_start.year + (cur_start.month // 12), (cur_start.month % 12) + 1
        ) and cur_end.day == 1:
            prev_end = cur_start
            if cur_start.month == 1:
                prev_start = date(cur_start.year - 1, 12, 1)
            else:
                prev_start = date(cur_start.year, cur_start.month - 1, 1)
        else:
            span = cur_end - cur_start
            prev_start, prev_end = cur_start - span, cur_start
        return prev_start, prev_end, cur_start, cur_end

    def _compile_explicit_comparison(self) -> Compiled:
        """Two explicit periods chosen by the model, no prev-period inference."""
        if self.metric.type != "aggregate":
            raise SemanticError("显式期间比较只支持可加聚合指标")
        comp = self.plan.comparison
        cur, base = comp.current, comp.baseline
        expr = self.metric.raw.get("expr")
        time_via = self.metric.raw.get("time_via")
        if not expr or not time_via:
            raise SemanticError(f"指标 {self.metric.id} 缺少 expr/time_via")
        dims = [f"{self._dim_column(d)} AS {_q(d)}" for d in self.plan.dimensions]
        period_tag = (
            f"CASE WHEN {self.phys(time_via)} >= {self.p(cur.start)} "
            f"AND {self.phys(time_via)} < {self.p(cur.end)} THEN 'current' "
            f"WHEN {self.phys(time_via)} >= {self.p(base.start)} "
            f"AND {self.phys(time_via)} < {self.p(base.end)} THEN 'baseline' END"
        )
        inner = dims + [f"{period_tag} AS {_q('_period')}", f"{expr} AS {_q('_v')}"]
        group_inner = [str(i + 1) for i in range(len(dims))] + [str(len(dims) + 1)]
        lo, hi = min(cur.start, base.start), max(cur.end, base.end)
        sql = [
            "WITH base AS (",
            "  SELECT " + ", ".join(inner),
            "  " + self._from_clause().replace("\n", "\n  "),
            "  " + self._where_clause(time_start=lo, time_end=hi).replace("\n", "\n  "),
            "  GROUP BY " + ", ".join(group_inner),
            "), pivoted AS (",
            "  SELECT " + ", ".join(
                [f"{_q(d)}" for d in self.plan.dimensions]
                + [
                    f"MAX(CASE WHEN {_q('_period')} = 'current' THEN {_q('_v')} END) AS {_q('current')}",
                    f"MAX(CASE WHEN {_q('_period')} = 'baseline' THEN {_q('_v')} END) AS {_q('baseline')}",
                ]
            ),
            "  FROM base",
            ("  GROUP BY " + ", ".join(str(i + 1) for i in range(len(self.plan.dimensions))))
            if self.plan.dimensions else "",
            ")",
            "SELECT ",
            "  " + ", ".join(
                [f"{_q(d)}" for d in self.plan.dimensions]
                + [
                    f"{_q('current')}",
                    f"{_q('baseline')}",
                    f"{_q('current')} - {_q('baseline')} AS {_q('delta')}",
                    f"CASE WHEN {_q('baseline')} IS NULL OR {_q('baseline')} = 0 THEN NULL "
                    f"ELSE ({_q('current')} - {_q('baseline')}) / {_q('baseline')} END AS {_q('growth_rate')}",
                ]
            ),
            "FROM pivoted",
        ]
        allowed = set(self.plan.dimensions) | {"value", "current", "baseline", "delta", "growth_rate"}
        order_parts = []
        for spec in self.plan.order_by:
            f = spec.field
            f = "current" if f in {self.plan.metric, "value"} else f
            if f not in allowed:
                raise SemanticError(f"无法按 {f} 排序")
            order_parts.append(f"{_q(f)} {spec.direction.upper()}")
        for d in self.plan.dimensions:
            if not any(s.field == d for s in self.plan.order_by):
                order_parts.append(f"{_q(d)} ASC")
        if order_parts:
            sql.append("ORDER BY " + ", ".join(order_parts))
        sql.append(f"LIMIT {int(self.plan.limit)}")
        columns = list(self.plan.dimensions) + ["current", "baseline", "delta", "growth_rate"]
        return self._finalize(
            "\n".join(x for x in sql if x),
            columns,
            {
                "comparison": "explicit",
                "comparison_label": comp.label,
                "current_range": [str(cur.start), str(cur.end)],
                "baseline_range": [str(base.start), str(base.end)],
            },
        )

    def _compile_compare(self) -> Compiled:
        if self.metric.type != "aggregate":
            raise SemanticError("环比/同比目前只支持可加聚合指标；比率指标请先查两期再比较")
        if not self.plan.time:
            raise SemanticError("环比/同比需要明确时间范围")
        expr = self.metric.raw.get("expr")
        if not expr:
            raise SemanticError(f"指标 {self.metric.id} 缺少 expr")
        prev_start, prev_end, cur_start, cur_end = self._period_bounds()
        time_via = self.metric.raw.get("time_via")
        dims = [f"{self._dim_column(d)} AS {_q(d)}" for d in self.plan.dimensions]
        period_tag = (
            f"CASE WHEN {self.phys(time_via)} >= {self.p(cur_start)} THEN 'current' "
            f"WHEN {self.phys(time_via)} >= {self.p(prev_start)} THEN 'baseline' END"
        )
        inner = dims + [f"{period_tag} AS {_q('_period')}", f"{expr} AS {_q('_v')}"]
        group_inner = [str(i + 1) for i in range(len(dims))] + [str(len(dims) + 1)]
        sql = [
            "WITH base AS (",
            "  SELECT " + ", ".join(inner),
            "  " + self._from_clause().replace("\n", "\n  "),
            "  " + self._where_clause(time_start=prev_start, time_end=cur_end).replace("\n", "\n  "),
            "  GROUP BY " + ", ".join(group_inner),
            "), pivoted AS (",
            "  SELECT " + ", ".join(
                [f"{_q(d)}" for d in self.plan.dimensions]
                + [
                    f"MAX(CASE WHEN {_q('_period')} = 'current' THEN {_q('_v')} END) AS {_q('current')}",
                    f"MAX(CASE WHEN {_q('_period')} = 'baseline' THEN {_q('_v')} END) AS {_q('baseline')}",
                ]
            ),
            "  FROM base",
            ("  GROUP BY " + ", ".join(str(i + 1) for i in range(len(self.plan.dimensions)))) if self.plan.dimensions else "",
            ")",
            "SELECT ",
            "  " + ", ".join([f"{_q(d)}" for d in self.plan.dimensions]
                            + [
                                f"{_q('current')}",
                                f"{_q('baseline')}",
                                f"{_q('current')} - {_q('baseline')} AS {_q('delta')}",
                                f"CASE WHEN {_q('baseline')} IS NULL OR {_q('baseline')} = 0 THEN NULL "
                                f"ELSE ({_q('current')} - {_q('baseline')}) / {_q('baseline')} END AS {_q('growth_rate')}",
                            ]),
            "FROM pivoted",
        ]
        allowed = set(self.plan.dimensions) | {"value", "current", "baseline", "delta", "growth_rate"}
        order_parts = []
        for spec in self.plan.order_by:
            f = spec.field
            f = "current" if f in {self.plan.metric, "value"} else f
            if f not in allowed:
                raise SemanticError(f"无法按 {f} 排序")
            order_parts.append(f"{_q(f)} {spec.direction.upper()}")
        for d in self.plan.dimensions:
            if not any(s.field == d for s in self.plan.order_by):
                order_parts.append(f"{_q(d)} ASC")
        if order_parts:
            sql.append("ORDER BY " + ", ".join(order_parts))
        sql.append(f"LIMIT {int(self.plan.limit)}")
        columns = list(self.plan.dimensions) + ["current", "baseline", "delta", "growth_rate"]
        return self._finalize("\n".join(x for x in sql if x), columns,
                              {"compare": self.plan.compare.type if self.plan.compare else None,
                               "current_range": [str(cur_start), str(cur_end)],
                               "baseline_range": [str(prev_start), str(prev_end)]})


def compile_plan(plan: QueryPlan, sem: SemanticLayer) -> Compiled:
    if plan.kind == "member_list":
        return _compile_member_list(plan, sem)
    return _Builder(plan, sem).compile()


# ---------- member list shape ----------
#
# Listing members is still a query with a scope: the SQL is generated from the
# semantic dimension declaration and the declared join graph, and every filter
# is a bound parameter. This is the *only* place list SQL is assembled.

def _join_graph(sem: SemanticLayer) -> dict[str, list[tuple[str, str, str]]]:
    """node -> [(neighbor, node_column, neighbor_column)]"""
    graph: dict[str, list[tuple[str, str, str]]] = {}
    for child, links in (sem.raw.get("joins") or {}).items():
        for parent, info in links.items():
            left = info.get("left") or info.get("on")
            right = info.get("right") or info.get("on")
            if not left or not right:
                continue
            graph.setdefault(child, []).append((parent, left, right))
            graph.setdefault(parent, []).append((child, right, left))
    return graph


def _bfs_from(graph: dict[str, list[tuple[str, str, str]]], base: str):
    from collections import deque

    prev: dict[str, tuple[str, str, str] | None] = {base: None}
    depth = {base: 0}
    queue = deque([base])
    while queue:
        node = queue.popleft()
        for nxt, node_col, nxt_col in graph.get(node, []):
            if nxt in prev:
                continue
            prev[nxt] = (node, node_col, nxt_col)
            depth[nxt] = depth[node] + 1
            queue.append(nxt)
    return prev, depth


def _path_edges(prev, target: str) -> list[tuple[str, str, str, str]]:
    """Return edges from base to target: (parent, child, parent_col, child_col)."""
    if target not in prev:
        raise SemanticError(f"join 图中找不到到 {target} 的路径")
    edges: list[tuple[str, str, str, str]] = []
    cur = target
    while prev[cur] is not None:
        parent, parent_col, child_col = prev[cur]
        edges.append((parent, cur, parent_col, child_col))
        cur = parent
    edges.reverse()
    return edges


def _compile_member_list(plan: QueryPlan, sem: SemanticLayer) -> Compiled:
    if not sem.capabilities.get("member_list", True):
        raise SemanticError("当前数据源未启用维度成员清单能力")
    dim = sem.dimension(plan.list_dim or "")
    raw = dim.raw
    base_ref = raw.get("column") or ""
    base_table, _, base_col = base_ref.partition(".")
    if not base_table or not base_col:
        raise SemanticError("成员清单维度缺少物理列")

    coverage = sem.capabilities.get("member_list_coverage") or {}
    coverage_fact = coverage.get("fact")
    coverage_key = coverage.get("key")
    coverage_label = coverage.get("label") or "记录数"
    coverage_time = coverage.get("time_column")

    graph = _join_graph(sem)
    prev, depth = _bfs_from(graph, base_table)

    filter_defs: list[tuple[str, str, object]] = []
    targets: set[str] = set()
    for fdim, spec in plan.filters.items():
        draw = sem.dimension(fdim).raw
        ref = draw.get("filter_column") or draw.get("column")
        if not ref:
            raise SemanticError(f"维度 {fdim} 不能作为过滤条件")
        ftable, _, fcol = ref.partition(".")
        if ftable == base_table:
            filter_defs.append((ftable, fcol, spec))
            continue
        targets.add(ftable)
        filter_defs.append((ftable, fcol, spec))

    if coverage_fact and coverage_fact != base_table:
        targets.add(coverage_fact)

    edge_order: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    inner: set[tuple[str, str]] = set()
    filter_targets = {t for t, _c, _s in filter_defs if t != base_table}
    for target in sorted(targets, key=lambda t: depth.get(t, 10**6)):
        for parent, child, parent_col, child_col in _path_edges(prev, target):
            key = (parent, child)
            if key not in seen:
                seen.add(key)
                edge_order.append((parent, child, parent_col, child_col))
            if target in filter_targets:
                inner.add(key)
    edge_order.sort(key=lambda e: depth.get(e[1], 0))

    alias: dict[str, str] = {base_table: "b"}
    for i, (_parent, child, _pc, _cc) in enumerate(edge_order, start=1):
        alias.setdefault(child, f"t{i}")

    params: list = []

    def phys(table: str, column: str) -> str:
        return f"{alias[table]}.{_q(sem.column(f'{table}.{column}'))}"

    def add_condition(target: str, spec) -> None:
        values = spec.value if isinstance(spec.value, list) else [spec.value]
        values = [v for v in values if v is not None]
        if not values:
            return
        if spec.op in {"eq", "in"}:
            where.append(f"{target} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        elif spec.op == "neq":
            where.append(f"{target} NOT IN ({', '.join('?' for _ in values)})")
            params.extend(values)
        else:
            symbol = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}.get(spec.op)
            if symbol:
                where.append(f"{target} {symbol} ?")
                params.append(values[0])

    joins: list[str] = []
    for parent, child, parent_col, child_col in edge_order:
        kind = "INNER" if (parent, child) in inner else "LEFT"
        cond = f"{phys(parent, parent_col)} = {phys(child, child_col)}"
        joins.append(f"  {kind} JOIN {_q(child)} {alias[child]} ON {cond}")

    where: list[str] = []
    for ftable, fcol, spec in filter_defs:
        add_condition(phys(ftable, fcol), spec)

    applied_time = False
    if plan.time:
        if coverage_fact and coverage_time:
            table, _, column = coverage_time.partition(".")
            where.append(
                f"{phys(table, column)} >= ? AND {phys(table, column)} < ?"
            )
            params.extend([str(plan.time.start), str(plan.time.end)])
            applied_time = True
        elif sem.tables.get(base_table, {}).get("time"):
            column = sem.tables[base_table]["time"]
            where.append(f"{phys(base_table, column)} >= ? AND {phys(base_table, column)} < ?")
            params.extend([str(plan.time.start), str(plan.time.end)])
            applied_time = True
        else:
            raise SemanticError(
                "该数据源的成员清单没有配置时间覆盖列，无法按时间筛选成员"
            )

    value_expr = phys(base_table, base_col)
    has_coverage = bool(coverage_fact and coverage_key)
    select = [f"{value_expr} AS \"value\""]
    columns = ["value"]
    if has_coverage:
        select.append(
            f"COUNT(DISTINCT {phys(coverage_fact, coverage_key)}) AS \"order_count\""
        )
        columns.append("order_count")

    order_parts: list[str] = []
    for spec in plan.order_by:
        target = "2" if spec.field in {"value", "order_count"} and has_coverage else "1"
        direction = "DESC" if spec.direction == "desc" else "ASC"
        order_parts.append(f"{target} {direction}")
    if not order_parts:
        order_parts = (["2 DESC", "1 ASC"] if has_coverage else ["1 ASC"])
    elif not any(p.startswith("1 ") for p in order_parts):
        order_parts.append("1 ASC")

    sql_parts = [
        "SELECT " + ", ".join(select),
        f"FROM {_q(base_table)} b",
        *joins,
    ]
    if where:
        sql_parts.append("WHERE " + " AND ".join(where))
    if has_coverage:
        sql_parts.append("GROUP BY 1")
    else:
        sql_parts[0] = "SELECT DISTINCT " + ", ".join(select)
    sql_parts += [
        "ORDER BY " + ", ".join(order_parts),
        f"LIMIT {int(plan.limit)}",
    ]

    return Compiled(
        sql="\n".join(sql_parts),
        params=params,
        columns=columns,
        applied_time_filter=applied_time,
        tables_used=sorted(set(alias)),
        meta={
            "lookup": plan.list_dim,
            "coverage": coverage_label if has_coverage else None,
            "range": [str(plan.time.start), str(plan.time.end)] if plan.time else None,
        },
    )
