"""Bounded, read-only tools used by the query harness.

The model never receives an unrestricted SQL tool. These tools expose the
semantic layer, database-authoritative member values, and plan validation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb

from app.domain.plan import HavingSpec, QueryPlan
from app.domain.semantic.loader import SemanticLayer
from app.domain.validator import validate_plan


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass
class ToolResult:
    name: str
    ok: bool
    data: Any = None
    error_code: str | None = None
    message: str = ""
    repairable: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "message": self.message,
            "repairable": self.repairable,
        }


@dataclass
class ToolHarness:
    sem: SemanticLayer
    warehouse: Any
    max_members: int = 50
    _cache: dict[str, list[str]] = field(default_factory=dict, init=False)

    def schema_search(self, query: str, limit: int = 8) -> ToolResult:
        needle = _norm(query)

        def score(identifier: str, name: str, synonyms: list[str]) -> int:
            words = [identifier, name, *synonyms]
            if needle in {_norm(word) for word in words if word}:
                return 100
            if needle and any(needle in _norm(word) for word in words if word):
                return 50
            return 0

        metrics = sorted(
            (
                (score(m.id, m.name, list(m.raw.get("synonyms") or [])), m)
                for m in self.sem.metrics.values()
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        dimensions = sorted(
            (
                (
                    score(
                        d.id,
                        str(d.raw.get("name_zh", d.id)),
                        list(d.raw.get("synonyms") or []),
                    ),
                    d,
                )
                for d in self.sem.dimensions.values()
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        return ToolResult(
            "schema.search",
            True,
            {
                "metrics": [
                    {
                        "id": metric.id,
                        "name": metric.name,
                        "type": metric.type,
                        "definition": metric.raw.get("definition"),
                    }
                    for metric_score, metric in metrics
                    if metric_score > 0
                ][:limit],
                "dimensions": [
                    {
                        "id": dim.id,
                        "name": dim.raw.get("name_zh", dim.id),
                        "kind": dim.kind,
                    }
                    for dim_score, dim in dimensions
                    if dim_score > 0
                ][:limit],
            },
        )

    def get_metric(self, metric_id: str | None) -> ToolResult:
        if not metric_id or metric_id not in self.sem.metrics:
            return ToolResult(
                "schema.get_metric",
                False,
                error_code="UNKNOWN_METRIC",
                message=f"数据库语义层中没有指标 {metric_id!r}",
                repairable=True,
            )
        metric = self.sem.metric(metric_id)
        return ToolResult(
            "schema.get_metric",
            True,
            {
                "id": metric.id,
                "name": metric.name,
                "type": metric.type,
                "definition": metric.raw.get("definition"),
                "unit": metric.raw.get("unit"),
                "supported_grain": list(metric.raw.get("supported_grain") or []),
                "time_via": metric.raw.get("time_via"),
            },
        )

    def get_dimension(self, dimension_id: str | None) -> ToolResult:
        if not dimension_id or dimension_id not in self.sem.dimensions:
            return ToolResult(
                "schema.get_dimension",
                False,
                error_code="UNKNOWN_DIMENSION",
                message=f"数据库语义层中没有维度 {dimension_id!r}",
                repairable=True,
            )
        dim = self.sem.dimension(dimension_id)
        return ToolResult(
            "schema.get_dimension",
            True,
            {
                "id": dim.id,
                "name": dim.raw.get("name_zh", dim.id),
                "kind": dim.kind,
                "column": dim.raw.get("column"),
                "synonyms": list(dim.raw.get("synonyms") or []),
            },
        )

    def lookup_members(self, dimension_id: str, term: str | None = None,
                       limit: int | None = None) -> ToolResult:
        if dimension_id not in self.sem.dimensions:
            return ToolResult(
                "members.lookup",
                False,
                error_code="UNKNOWN_DIMENSION",
                message=f"数据库语义层中没有维度 {dimension_id!r}",
                repairable=True,
            )
        if dimension_id not in self._cache:
            dim = self.sem.dimension(dimension_id)
            ref = dim.raw.get("filter_lookup") or dim.raw.get("column")
            if not ref:
                return ToolResult(
                    "members.lookup",
                    False,
                    error_code="UNSUPPORTED_MEMBER_LIST",
                    message="该维度没有可读取的物理列",
                    repairable=False,
                )
            table, _, logical = str(ref).partition(".")
            if not table or not logical:
                return ToolResult(
                    "members.lookup",
                    False,
                    error_code="INVALID_DIMENSION_COLUMN",
                    message="维度物理列配置无效",
                    repairable=False,
                )
            column = self.sem.column(ref)
            con = duckdb.connect(str(self.warehouse), read_only=True)
            try:
                rows = con.execute(
                    f"SELECT DISTINCT {_quote(column)} "
                    f"FROM {_quote(table)} "
                    f"WHERE {_quote(column)} IS NOT NULL "
                    f"ORDER BY {_quote(column)}"
                ).fetchall()
            finally:
                con.close()
            self._cache[dimension_id] = [str(row[0]) for row in rows]

        values = self._cache[dimension_id]
        needle = _norm(term)
        if needle:
            exact = [value for value in values if _norm(value) == needle]
            partial = [
                value for value in values
                if needle in _norm(value) or _norm(value) in needle
            ]
            matches = exact or partial
            status = (
                "exact" if exact
                else "partial" if partial
                else "not_found"
            )
        else:
            matches = values
            status = "list"
        cap = min(int(limit or self.max_members), self.max_members)
        return ToolResult(
            "members.lookup",
            True,
            {
                "dimension_id": dimension_id,
                "status": status,
                "matches": matches[:cap],
                "total": len(matches),
                "truncated": len(matches) > cap,
                "authority": "database",
            },
        )

    def validate(self, plan: QueryPlan) -> ToolResult:
        issues = validate_plan(plan, self.sem)
        errors = [issue for issue in issues if issue.severity == "error"]
        return ToolResult(
            "plan.validate",
            not errors,
            {
                "issues": [issue.as_dict() for issue in issues],
            },
            error_code=errors[0].code if errors else None,
            message=errors[0].message if errors else "",
            repairable=bool(errors),
        )

    def repair_nested_plan(self, plan: QueryPlan,
                           issues: list[dict]) -> tuple[QueryPlan | None, dict]:
        issue_codes = {issue.get("code") for issue in issues}
        if "NESTED_UNSUPPORTED" not in issue_codes or not plan.nested:
            return None, {}
        repaired = plan.model_copy(deep=True)
        minimum = int(repaired.nested.min_orders)
        repaired.nested = None
        if not any(
            spec.metric == "order_count" and spec.op == "gte"
            for spec in repaired.having
        ):
            repaired.having.append(
                HavingSpec(metric="order_count", op="gte", value=minimum)
            )
        return repaired, {
            "tool": "plan.repair",
            "rule": "nested_min_orders_to_having",
            "removed_nested": True,
            "having": [
                spec.model_dump(mode="json") for spec in repaired.having
            ],
        }

    def infer_count_member_dimension(self, metric_id: str | None) -> str | None:
        if not metric_id or metric_id not in self.sem.metrics:
            return None
        metric = self.sem.metrics[metric_id]
        explicit = metric.raw.get("member_dimension")
        if explicit in self.sem.dimensions:
            return explicit
        expr = str(metric.raw.get("expr") or "").upper()
        if not expr.startswith("COUNT(DISTINCT"):
            return None
        metric_name = _norm(metric.name)
        candidates: list[tuple[int, str]] = []
        for dim_id in metric.supported_grain:
            dim = self.sem.dimensions.get(dim_id)
            if dim is None or dim.kind in {"time", "derived"}:
                continue
            name = _norm(dim.raw.get("name_zh", ""))
            base = name.removesuffix("名称")
            if name and (name in metric_name or base in metric_name):
                candidates.append((len(name), dim_id))
        return min(candidates)[1] if candidates else None
