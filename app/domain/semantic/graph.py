"""Derived capability graph.

The semantic layer declares metrics and dimensions; this module computes what
each metric can actually be combined with, from the join graph:

- N:1-reachable dimensions are always safe;
- dimensions only reachable through 1:N joins are "fanout" dimensions: safe for
  DISTINCT counts / ratios / entity counts, unsafe for SUM/AVG/COUNT(*).

This is the single source of truth for answerability, schema cards and
validator membership. supported_grain remains in the YAML as a hint/legacy
field, but it is no longer trusted on its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.semantic.loader import SemanticLayer


def fanout_safe(metric: Any) -> bool:
    """Whether a 1:N join cannot double-count this metric."""
    if metric.type in {"ratio", "entity_count"}:
        return True
    expr = " ".join(
        str(metric.raw.get(key) or "")
        for key in ("expr", "numerator", "count_orders")
    ).upper()
    return "DISTINCT" in expr


def _n1_tables(sem: SemanticLayer, start: str) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        for parent in (sem.joins.get(node) or {}):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _all_tables(sem: SemanticLayer, start: str) -> set[str]:
    graph: dict[str, set[str]] = {}
    for child, parents in sem.joins.items():
        for parent in parents:
            graph.setdefault(child, set()).add(parent)
            graph.setdefault(parent, set()).add(child)
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        for nxt in graph.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _dimension_table(kind: str, raw: dict) -> str:
    if kind == "derived":
        return str(raw.get("requires") or "")
    column = str(raw.get("column") or "")
    return column.split(".", 1)[0] if "." in column else ""


@dataclass(frozen=True)
class MetricCapability:
    metric_id: str
    fact: str
    dimensions: frozenset[str]
    fanout_dimensions: frozenset[str]
    allowed_dimensions: frozenset[str]
    time_column: str | None
    time_axis: str | None
    time_coverage: tuple[str, str] | None

    def as_dict(self) -> dict:
        return {
            "fact": self.fact,
            "dimensions": sorted(self.dimensions),
            "fanout_dimensions": sorted(self.fanout_dimensions),
            "allowed_dimensions": sorted(self.allowed_dimensions),
            "time_column": self.time_column,
            "time_axis": self.time_axis,
            "time_coverage": list(self.time_coverage) if self.time_coverage else None,
        }


def build_capabilities(sem: SemanticLayer) -> dict[str, MetricCapability]:
    capabilities: dict[str, MetricCapability] = {}
    for metric_id, metric in sem.metrics.items():
        fact = str(metric.raw.get("fact") or "")
        if not fact or fact not in sem.tables:
            continue
        n1 = _n1_tables(sem, fact)
        all_tables = _all_tables(sem, fact)
        safe: set[str] = set()
        fanout: set[str] = set()
        for dim_id, dim in sem.dimensions.items():
            kind = dim.kind
            if kind == "time":
                if dim_id == "month" and metric.raw.get("time_via"):
                    safe.add(dim_id)
                continue
            table = _dimension_table(kind, dim.raw)
            if not table:
                continue
            if table in n1:
                safe.add(dim_id)
            elif table in all_tables:
                fanout.add(dim_id)
        allowed = set(safe)
        if fanout_safe(metric):
            allowed |= fanout
        health = metric.raw.get("data_health") or {}
        axis_ref = metric.raw.get("time_via")
        axis_coverage = next(
            (
                tuple(axis["coverage"])
                for axis in (sem.time_axes or [])
                if axis.get("ref") == axis_ref and axis.get("coverage")
            ),
            None,
        )
        coverage = (
            axis_coverage
            or (
                tuple(health["time_coverage"])
                if isinstance(health, dict) and health.get("time_coverage")
                else (tuple(sem.time_range) if sem.time_range else None)
            )
        )
        capabilities[metric_id] = MetricCapability(
            metric_id=metric_id,
            fact=fact,
            dimensions=frozenset(safe),
            fanout_dimensions=frozenset(fanout),
            allowed_dimensions=frozenset(allowed),
            time_column=metric.raw.get("time_via"),
            time_axis=metric.raw.get("time_axis"),
            time_coverage=coverage,
        )
    return capabilities


def common_grain_table(sem: SemanticLayer, fact_a: str, fact_b: str) -> str | None:
    """Find an N:1 common table with a declared grain for a cross-fact ratio."""
    if not fact_a or not fact_b:
        return None
    common = _n1_tables(sem, fact_a) & _n1_tables(sem, fact_b)
    candidates = [
        table for table in common
        if ((sem.tables.get(table) or {}).get("grain") or [])
    ]
    if not candidates:
        return None
    for preferred in (fact_b, fact_a):
        if preferred in candidates:
            return preferred
    # Prefer the most granular common grain. Alphabetical order picked a
    # higher-level table such as customers over orders, which then forced a
    # 1:N join back to orders for the time filter.
    closures = {
        table: _n1_tables(sem, table) & set(candidates)
        for table in candidates
    }
    return max(candidates, key=lambda table: (len(closures[table]), table))


def time_axis_allowed(sem: SemanticLayer, metric: Any, ref: str) -> bool:
    """A time axis must be a real time column reachable via N:1 joins."""
    table, _, column = str(ref or "").partition(".")
    if not table or not column or table not in sem.tables:
        return False
    columns = (sem.tables.get(table) or {}).get("columns") or []
    if column not in columns:
        return False
    declared = {str(axis.get("ref")) for axis in (sem.time_axes or [])}
    if declared and str(ref) not in declared:
        return False
    fact = str(metric.raw.get("fact") or "")
    if not fact or fact not in sem.tables:
        return False
    return table in _n1_tables(sem, fact)


def allowed_dimensions(sem: SemanticLayer, metric_id: str) -> set[str]:
    capability = build_capabilities(sem).get(metric_id)
    if capability is None:
        metric = sem.metrics.get(metric_id)
        return set(metric.raw.get("supported_grain") or ["none"]) if metric else {"none"}
    return set(capability.allowed_dimensions) | {"none"}
