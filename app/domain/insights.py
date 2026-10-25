"""Deterministic, zero-LLM insights derived from a result set.

These are the always-on "key facts" attached to every answer. They never
introduce a number that is not in `rows`; the goal is explainability, not
prose. The deep analysis report builds on the same function.
"""
from __future__ import annotations

from typing import Any

from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer


def _fmt(value: Any, unit: str | None = None) -> str:
    if value is None:
        return "—"
    if unit == "%":
        return f"{float(value) * 100:.2f}%"
    if isinstance(value, float):
        if unit in {"元", "分"}:
            return f"{value:,.2f}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:+.2f}%"


def _dim_name(row: list, dims: list[str], sem: SemanticLayer) -> str:
    if not dims:
        return "—"
    return " / ".join(str(v) for v in row[: len(dims)])


def _col(cols: list[str], name: str) -> int | None:
    return cols.index(name) if name in cols else None


def build_insights(plan: QueryPlan, columns: list[str], rows: list[list],
                   row_count: int, truncated: bool, issues: list[dict],
                   sem: SemanticLayer, max_items: int = 3) -> list[str]:
    if not rows:
        return []
    metric = sem.metric(plan.metric) if plan.metric else None
    unit = metric.raw.get("unit") if metric else None
    name = metric.name if metric else (plan.list_dim or "结果")
    out: list[str] = []

    # ---- dimension member list ----
    if plan.kind == "member_list":
        out.append(f"清单共 {row_count} 个成员" + ("（已截断）" if truncated else ""))
        if "order_count" in columns:
            oi = columns.index("order_count")
            top = sorted(rows, key=lambda r: (r[oi] or 0), reverse=True)[:3]
            out.append("覆盖订单数前三：" + "、".join(
                f"{_dim_name(r, plan.dimensions or [plan.list_dim or ''], sem)}（{r[oi]}）"
                for r in top
            ))
        return out[:max_items]

    ci = _col(columns, "current")
    bi = _col(columns, "baseline")
    di = _col(columns, "delta")
    gi = _col(columns, "growth_rate")
    dims = plan.dimensions

    # ---- period vs period ----
    if ci is not None and bi is not None and di is not None:
        if dims:
            def key(r):
                return float("inf") if r[di] is None else float(r[di])
            pos = max(rows, key=key)
            neg = min(rows, key=key)
            if pos[di] is not None and float(pos[di]) > 0:
                out.append(
                    f"增量最大：{_dim_name(pos, dims, sem)}，"
                    f"{_fmt(pos[di], unit)}（{_pct(pos[gi]) if gi is not None else '—'}）"
                )
            if (neg is not pos and neg[di] is not None and float(neg[di]) < 0):
                out.append(
                    f"拖累最大：{_dim_name(neg, dims, sem)}，"
                    f"{_fmt(neg[di], unit)}（{_pct(neg[gi]) if gi is not None else '—'}）"
                )
            if metric and metric.raw.get("additive"):
                total = sum(float(r[di]) for r in rows if r[di] is not None)
                out.append(f"所选分组增量合计：{_fmt(total, unit)}")
        else:
            row = rows[0]
            out.append(
                f"本期 {_fmt(row[ci], unit)} → 基期 {_fmt(row[bi], unit)}，"
                f"变化 {_fmt(row[di], unit)}"
                + (f"（{_pct(row[gi])}）" if gi is not None else "")
            )
        return out[:max_items]

    # ---- trend by month ----
    vi = _col(columns, "value")
    if vi is not None and "month" in dims and len(rows) >= 2:
        ordered = sorted(rows, key=lambda r: str(r[0]))
        first, last = ordered[0], ordered[-1]
        hi = max(ordered, key=lambda r: (r[vi] if r[vi] is not None else float("-inf")))
        lo = min(ordered, key=lambda r: (r[vi] if r[vi] is not None else float("inf")))
        change = None
        if first[vi] not in (None, 0):
            change = (float(last[vi]) - float(first[vi])) / float(first[vi])
        out.append(
            f"近 {len(ordered)} 个月：{first[0]} {_fmt(first[vi], unit)} → "
            f"{last[0]} {_fmt(last[vi], unit)}"
            + (f"（{_pct(change)}）" if change is not None else "")
        )
        out.append(f"最高 {hi[0]}（{_fmt(hi[vi], unit)}），最低 {lo[0]}（{_fmt(lo[vi], unit)}）")
        return out[:max_items]

    # ---- ranking / single value ----
    if vi is not None:
        if dims:
            vals = [float(r[vi]) for r in rows if r[vi] is not None]
            if metric and metric.raw.get("additive") and sum(vals) > 0:
                total = sum(vals)
                top = max(rows, key=lambda r: (r[vi] if r[vi] is not None else float("-inf")))
                top3 = sum(sorted(vals, reverse=True)[:3])
                out.append(
                    f"Top1 {_dim_name(top, dims, sem)} 占比 "
                    f"{float(top[vi]) / total * 100:.1f}%；Top3 合计 {top3 / total * 100:.1f}%"
                )
            else:
                top = max(rows, key=lambda r: (r[vi] if r[vi] is not None else float("-inf")))
                out.append(f"最高：{_dim_name(top, dims, sem)}（{_fmt(top[vi], unit)}）")
        else:
            row = rows[0]
            ni = _col(columns, "numerator")
            de = _col(columns, "denominator")
            if ni is not None and de is not None:
                out.append(
                    f"{name} = {_fmt(row[ni], None)} / {_fmt(row[de], None)} = "
                    f"{_fmt(row[vi], unit)}"
                )
            else:
                out.append(f"{name}：{_fmt(row[vi], unit)}")
        if truncated:
            out.append(f"结果已截断，仅展示前 {row_count} 行")
        return out[:max_items]

    return out[:max_items]
