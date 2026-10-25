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

from app.domain.plan import ComparisonSpec, QueryPlan, TimeRange
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
        needed |= self._expr_tables(self.metric.raw.get("time_via", ""))
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

        adj: dict[str, list[tuple[str, str, str]]] = {}
        for src, targets in self.sem.joins.items():
            for dst, spec in targets.items():
                adj.setdefault(src, []).append((dst, spec["on"], "forward"))
                adj.setdefault(dst, []).append((src, spec["on"], "reverse"))

        needed = self._needed_tables()
        seen = {fact}
        queue = deque([fact])
        joins: list[str] = []
        while queue:
            node = queue.popleft()
            for dst, on, direction in adj.get(node, []):
                if dst in seen:
                    continue
                if dst not in needed:
                    continue
                seen.add(dst)
                if direction == "forward":
                    joins.append(f"JOIN {_q(dst)} ON {_q(node)}.{_q(on)} = {_q(dst)}.{_q(on)}")
                else:
                    joins.append(f"JOIN {_q(dst)} ON {_q(dst)}.{_q(on)} = {_q(node)}.{_q(on)}")
                queue.append(dst)
        for line in joins:
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
        sql.append(self._having_clause({"order_count": "COUNT(DISTINCT orders.order_id)"}))
        order = self._order_clause(set(self.plan.dimensions) | {"value"})
        if order:
            sql.append(order)
        sql.append(f"LIMIT {int(self.plan.limit)}")
        columns = list(self.plan.dimensions) + ["value"]
        return self._finalize("\n".join(x for x in sql if x), columns)

    def _compile_ratio(self) -> Compiled:
        grain_key_name = self.plan.ratio_grain or self.metric.raw.get("ratio_grain") or self.sem.policies.get("ratio_grain", "order")
        key_map = {"order": "orders.order_id", "review": "order_reviews.review_id"}
        if grain_key_name not in key_map:
            raise SemanticError(f"未知 ratio_grain: {grain_key_name}")
        key = self.phys(key_map[grain_key_name])
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
        if self.metric.type == "ratio":
            return self._compile_explicit_ratio_comparison()
        if self.metric.type != "aggregate":
            raise SemanticError("显式期间比较只支持可加聚合指标和比率指标")
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

    def _compile_explicit_ratio_comparison(self, explicit: bool = True) -> Compiled:
        """Two periods for a ratio metric (explicit or inferred prev-period).

        The ratio must be built from per-period grain sums (numerator and
        denominator aggregated separately), never by averaging per-row ratios.
        """
        comp = self.plan.comparison
        cur, base = comp.current, comp.baseline
        time_via = self.metric.raw.get("time_via")
        grain_key_name = (self.plan.ratio_grain
                          or self.metric.raw.get("ratio_grain")
                          or self.sem.policies.get("ratio_grain", "order"))
        key_map = {"order": "orders.order_id", "review": "order_reviews.review_id"}
        if grain_key_name not in key_map:
            raise SemanticError(f"未知 ratio_grain: {grain_key_name}")
        key = self.phys(key_map[grain_key_name])
        num = self.metric.raw.get("grain_numerator")
        den = self.metric.raw.get("grain_denominator")
        if not time_via or not num or not den:
            raise SemanticError(f"比率指标 {self.metric.id} 缺少 time_via/grain_numerator/denominator")

        dims = self._dims_select()
        dim_names = [f"{_q(d)}" for d in self.plan.dimensions]
        period_tag = (
            f"CASE WHEN {self.phys(time_via)} >= {self.p(cur.start)} "
            f"AND {self.phys(time_via)} < {self.p(cur.end)} THEN 'current' "
            f"WHEN {self.phys(time_via)} >= {self.p(base.start)} "
            f"AND {self.phys(time_via)} < {self.p(base.end)} THEN 'baseline' END"
        )
        inner = dims + [
            f"{period_tag} AS {_q('_period')}",
            f"{key} AS {_q('_key')}",
            f"{num} AS {_q('_num')}",
            f"{den} AS {_q('_den')}",
        ]
        group_inner = [str(i + 1) for i in range(len(dims))]
        group_inner += [str(len(dims) + 1), str(len(dims) + 2)]  # _period, _key
        group_agg = [str(i + 1) for i in range(len(dims))] + [str(len(dims) + 1)]
        lo, hi = min(cur.start, base.start), max(cur.end, base.end)

        sql = [
            "WITH grain AS (",
            "  SELECT " + ", ".join(inner),
            "  " + self._from_clause().replace("\n", "\n  "),
            "  " + self._where_clause(time_start=lo, time_end=hi).replace("\n", "\n  "),
            "  GROUP BY " + ", ".join(group_inner),
            "), agg AS (",
            "  SELECT " + ", ".join(
                dim_names
                + [
                    f"{_q('_period')}",
                    f"SUM({_q('_num')}) AS {_q('_num')}",
                    f"SUM({_q('_den')}) AS {_q('_den')}",
                ]
            ),
            "  FROM grain",
            "  GROUP BY " + ", ".join(group_agg),
            "), pivoted AS (",
            "  SELECT " + ", ".join(
                dim_names
                + [
                    f"MAX(CASE WHEN {_q('_period')} = 'current' THEN {_q('_num')} END) AS {_q('current_num')}",
                    f"MAX(CASE WHEN {_q('_period')} = 'current' THEN {_q('_den')} END) AS {_q('current_den')}",
                    f"MAX(CASE WHEN {_q('_period')} = 'baseline' THEN {_q('_num')} END) AS {_q('baseline_num')}",
                    f"MAX(CASE WHEN {_q('_period')} = 'baseline' THEN {_q('_den')} END) AS {_q('baseline_den')}",
                ]
            ),
            "  FROM agg",
            ("  GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims))))
            if dims else "",
            ")",
            "SELECT ",
            "  " + ", ".join(
                dim_names
                + [
                    f"{_q('current_num')} / NULLIF({_q('current_den')}, 0) AS {_q('current')}",
                    f"{_q('baseline_num')} / NULLIF({_q('baseline_den')}, 0) AS {_q('baseline')}",
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
                "comparison": "explicit" if explicit else "implicit",
                "comparison_label": comp.label,
                "current_range": [str(cur.start), str(cur.end)],
                "baseline_range": [str(base.start), str(base.end)],
                "ratio_grain": grain_key_name,
            },
        )

    def _compile_compare(self) -> Compiled:
        if self.metric.type == "ratio":
            if not self.plan.time:
                raise SemanticError("环比/同比需要明确时间范围")
            prev_start, prev_end, cur_start, cur_end = self._period_bounds()
            label = ("同比" if self.plan.compare and self.plan.compare.type == "prev_year"
                     else "环比")
            self.plan = self.plan.model_copy(deep=True)
            self.plan.comparison = ComparisonSpec(
                label=f"{label}（{cur_start} 至 {cur_end} vs {prev_start} 至 {prev_end}）",
                current=TimeRange(start=cur_start, end=cur_end),
                baseline=TimeRange(start=prev_start, end=prev_end),
            )
            return self._compile_explicit_ratio_comparison(explicit=False)
        if self.metric.type != "aggregate":
            raise SemanticError("环比/同比目前只支持可加聚合指标和比率指标")
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

def _join_graph(sem: SemanticLayer) -> dict[str, list[tuple[str, str]]]:
    graph: dict[str, list[tuple[str, str]]] = {}
    for child, links in (sem.raw.get("joins") or {}).items():
        for parent, info in links.items():
            on = info.get("on")
            if not on:
                continue
            graph.setdefault(child, []).append((parent, on))
            graph.setdefault(parent, []).append((child, on))
    return graph


def _bfs_from(graph: dict[str, list[tuple[str, str]]], base: str):
    from collections import deque

    prev: dict[str, tuple[str, str] | None] = {base: None}
    depth = {base: 0}
    queue = deque([base])
    while queue:
        node = queue.popleft()
        for nxt, on in graph.get(node, []):
            if nxt in prev:
                continue
            prev[nxt] = (node, on)
            depth[nxt] = depth[node] + 1
            queue.append(nxt)
    return prev, depth


def _path_edges(prev, target: str) -> list[tuple[str, str, str]]:
    if target not in prev:
        raise SemanticError(f"join 图中找不到到 {target} 的路径")
    edges: list[tuple[str, str, str]] = []
    cur = target
    while prev[cur] is not None:
        parent, on = prev[cur]
        edges.append((parent, cur, on))
        cur = parent
    return edges


def _compile_member_list(plan: QueryPlan, sem: SemanticLayer) -> Compiled:
    dim = sem.dimension(plan.list_dim or "")
    raw = dim.raw
    base_ref = raw.get("column") or ""
    base_table, _, base_col = base_ref.partition(".")

    graph = _join_graph(sem)
    prev, depth = _bfs_from(graph, base_table)

    # Filters are part of the member scope; orders is always needed for the
    # coverage column.
    filter_defs: list[tuple[str, str, object]] = []
    filter_targets: set[str] = set()
    for fdim, spec in plan.filters.items():
        draw = sem.dimension(fdim).raw
        ref = draw.get("filter_column") or draw.get("column")
        if not ref:
            raise SemanticError(f"维度 {fdim} 不能作为过滤条件")
        ftable, _, fcol = ref.partition(".")
        filter_targets.add(ftable)
        filter_defs.append((ftable, fcol, spec))

    targets = set(filter_targets)
    targets.add("orders")

    # Collect the join tree and remember which edges are required by a filter
    # or by the time window (those are INNER); the coverage-only path to orders
    # stays LEFT so zero-coverage members still appear.
    edge_order: list[tuple[str, str, str]] = []
    seen: set[frozenset] = set()
    inner: set[frozenset] = set()
    for target in sorted(targets, key=lambda t: depth.get(t, 10**6)):
        for parent, child, on in _path_edges(prev, target):
            key = frozenset((parent, child))
            if key not in seen:
                seen.add(key)
                edge_order.append((parent, child, on))
            if target in filter_targets or (plan.time and target == "orders"):
                inner.add(key)
    edge_order.sort(key=lambda e: depth.get(e[1], 0))

    # Stable aliases: base = b, then every joined table in join order.
    alias: dict[str, str] = {base_table: "b"}
    for i, (_parent, child, _on) in enumerate(edge_order, start=1):
        alias.setdefault(child, f"t{i}")
    if "orders" not in alias:
        alias["orders"] = "b"  # base table is orders

    params: list = []

    def col(table: str, logical: str) -> str:
        return f"{alias[table]}.{_q(sem.column(f'{table}.{logical}'))}"

    value_expr = f"{alias[base_table]}.{_q(sem.column(base_ref))}"

    joins: list[str] = []
    for parent, child, on in edge_order:
        kind = "INNER" if frozenset((parent, child)) in inner else "LEFT"
        cond = f"{col(parent, on)} = {col(child, on)}"
        if child == "orders":
            status_col = sem.column("orders.order_status")
            cond += f" AND {alias['orders']}.{_q(status_col)} = ?"
            params.append("delivered")
            if plan.time:
                time_col = sem.column(f"orders.{sem.tables['orders'].get('time', 'purchase_at')}")
                cond += f" AND {alias['orders']}.{_q(time_col)} >= ? AND {alias['orders']}.{_q(time_col)} < ?"
                params.extend([str(plan.time.start), str(plan.time.end)])
        joins.append(f"  {kind} JOIN {_q(child)} {alias[child]} ON {cond}")

    where: list[str] = []
    for ftable, fcol, spec in filter_defs:
        target = f"{alias[ftable]}.{_q(sem.column(f'{ftable}.{fcol}'))}"
        values = spec.value if isinstance(spec.value, list) else [spec.value]
        values = [v for v in values if v is not None]
        if not values:
            continue
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

    orders_joined = any(child == "orders" for _p, child, _o in edge_order)
    if plan.time and not orders_joined:
        # base table is orders: apply the time window to the base rows.
        time_col = sem.column(f"orders.{sem.tables['orders'].get('time', 'purchase_at')}")
        where.append(f"b.{_q(time_col)} >= ? AND b.{_q(time_col)} < ?")
        params.extend([str(plan.time.start), str(plan.time.end)])

    if base_table == "orders":
        status_col = sem.column("orders.order_status")
        where.insert(0, f"b.{_q(status_col)} = ?")
        params.insert(0, "delivered")

    order_parts: list[str] = []
    for spec in plan.order_by:
        target = "2" if spec.field in {"value", "order_count"} else "1"
        direction = "DESC" if spec.direction == "desc" else "ASC"
        order_parts.append(f"{target} {direction}")
    if not order_parts:
        order_parts = ["2 DESC", "1 ASC"]
    elif not any(p.startswith("1 ") for p in order_parts):
        order_parts.append("1 ASC")

    sql_parts = [
        f"SELECT {value_expr} AS \"value\", "
        f"COUNT(DISTINCT {alias['orders']}.{_q(sem.column('orders.order_id'))}) AS \"order_count\"",
        f"FROM {_q(base_table)} b",
        *joins,
    ]
    if where:
        sql_parts.append("WHERE " + " AND ".join(where))
    sql_parts += [
        "GROUP BY 1",
        "ORDER BY " + ", ".join(order_parts),
        f"LIMIT {int(plan.limit)}",
    ]

    return Compiled(
        sql="\n".join(sql_parts),
        params=params,
        columns=["value", "order_count"],
        applied_time_filter=bool(plan.time),
        tables_used=sorted(set(alias)),
        meta={
            "lookup": plan.list_dim,
            "coverage": "delivered_orders",
            "range": [str(plan.time.start), str(plan.time.end)] if plan.time else None,
        },
    )
