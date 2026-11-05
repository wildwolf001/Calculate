"""Query-aware data quality checks.

These are deterministic scans over the warehouse, not LLM guesses. They run
after the main query and return structured issues that the composer can render.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from app.config.settings import WAREHOUSE_PATH
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@dataclass
class QualityIssue:
    code: str
    severity: str
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    impact: str = ""
    suggestion: str = ""


def _month_keys(start: date, end: date) -> list[str]:
    current = date(start.year, start.month, 1)
    last = end - timedelta(days=1)
    out = []
    while current <= last:
        out.append(current.strftime("%Y-%m"))
        current = date(
            current.year + (current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
        )
    return out


def _join_path(sem: SemanticLayer, start: str, target: str) -> tuple[str, str] | None:
    if start == target:
        return "", start
    from collections import deque

    adj: dict[str, list[tuple[str, str, str]]] = {}
    for src, children in sem.joins.items():
        for dst, spec in children.items():
            left = spec.get("left")
            right = spec.get("right")
            if left and right:
                adj.setdefault(src, []).append((dst, left, right))
    parent: dict[str, tuple[str, str, str] | None] = {start: None}
    queue = deque([start])
    while queue and target not in parent:
        node = queue.popleft()
        for dst, left, right in adj.get(node, []):
            if dst in parent:
                continue
            parent[dst] = (node, left, right)
            queue.append(dst)
    if target not in parent:
        return None
    steps = []
    cur = target
    while parent[cur] is not None:
        node, left, right = parent[cur]
        steps.append((node, cur, left, right))
        cur = node
    steps.reverse()
    joins = []
    for node, dst, left, right in steps:
        joins.append(
            f"JOIN {_q(dst)} ON {_q(node)}.{_q(sem.column(f'{node}.{left}'))} "
            f"= {_q(dst)}.{_q(sem.column(f'{dst}.{right}'))}"
        )
    return "\n  ".join(joins), target


def _policy_issue(plan: QueryPlan, sem: SemanticLayer) -> QualityIssue | None:
    policy = (sem.raw or {}).get("policy") or {}
    bounds = policy.get("time_range") or []
    if not plan.time or len(bounds) != 2:
        return None
    try:
        p_start, p_end = (date.fromisoformat(str(item)) for item in bounds)
    except ValueError:
        return None
    start, end = plan.time.start, plan.time.end - timedelta(days=1)
    if start < p_start or end > p_end:
        return QualityIssue(
            "POLICY_RANGE", "warn", "请求超出 README 数据范围",
            f"README 声明的数据范围为 {p_start} 至 {p_end}，"
            f"本次请求覆盖 {start} 至 {end}，超出部分可能没有数据或不可比。",
            {"requested": [str(start), str(end)],
             "policy_range": [str(p_start), str(p_end)]},
            "超出权威范围的时间段可能为空或不完整。",
            "按 README 范围裁剪，或确认数据是否已更新。",
        )
    return None


def _coverage(plan: QueryPlan, metric, sem: SemanticLayer, con) -> list[QualityIssue]:
    if not plan.time:
        return []
    time_ref = plan.time_axis or metric.raw.get("time_via")
    if not isinstance(time_ref, str) or "." not in time_ref:
        return []
    table, _, column = time_ref.partition(".")
    path = _join_path(sem, metric.fact, table)
    if path is None:
        return []
    joins, _ = path
    time_col = f"{_q(table)}.{_q(sem.column(time_ref))}"
    sql = (
        f"SELECT strftime(CAST({time_col} AS DATE), '%Y-%m') AS month, "
        f"COUNT(*) AS n, MAX({time_col}) AS max_time "
        f"FROM {_q(metric.fact)}"
        + (f"\n  {joins}" if joins else "")
        + f"\n  WHERE {time_col} >= ? AND {time_col} < ? GROUP BY 1"
    )
    try:
        rows = con.execute(sql, [plan.time.start, plan.time.end]).fetchall()
    except Exception:
        return []
    actual = {str(row[0]): row[1] for row in rows}
    expected = _month_keys(plan.time.start, plan.time.end)
    missing = [month for month in expected if month not in actual]
    issues: list[QualityIssue] = []
    if missing:
        issues.append(QualityIssue(
            "TIME_GAP", "warn", "存在缺失月份",
            f"期望覆盖 {len(expected)} 个月，实际有记录 {len(actual)} 个月；"
            f"缺失：{'、'.join(missing)}。",
            {"expected_months": expected, "missing_months": missing},
            "缺失月份会让期间合计偏低，不能直接与完整期间比较。",
            "如需完整期间，请补齐数据或改用实际覆盖区间。",
        ))
    max_time = max((row[2] for row in rows if row[2]), default=None)
    if max_time is not None:
        end_date = plan.time.end - timedelta(days=1)
        max_date = max_time.date() if hasattr(max_time, "date") else max_time
        if max_date < end_date and expected and expected[-1] in actual:
            issues.append(QualityIssue(
                "PARTIAL_PERIOD", "warn", "末尾月份不完整",
                f"请求区间最后一个月只覆盖到 {max_date}，"
                f"不是完整月份。",
                {"last_data_date": str(max_date), "period_end": str(end_date)},
                "末尾月份的业务量可能偏低，趋势/环比需谨慎。",
                "可把末尾月份标记为部分月份，或查询完整月份。",
            ))
    return issues


def _metric_refs(metric) -> list[str]:
    text = " ".join(
        str(metric.raw.get(key) or "")
        for key in ("expr", "numerator", "denominator")
    )
    refs = re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", text)
    return list(dict.fromkeys(f"{table}.{col}" for table, col in refs))


def _value_health(metric, sem: SemanticLayer, con) -> list[QualityIssue]:
    issues: list[QualityIssue] = []
    for ref in _metric_refs(metric):
        table, _, column = ref.partition(".")
        if table not in sem.tables:
            continue
        physical = sem.column(ref)
        expr = f"{_q(table)}.{_q(physical)}"
        try:
            row = con.execute(
                f"SELECT COUNT(*) FILTER (WHERE {expr} < 0), "
                f"COALESCE(SUM(CASE WHEN {expr} < 0 THEN {expr} ELSE 0 END), 0), "
                f"COUNT(*) FILTER (WHERE {expr} IS NULL), COUNT(*) "
                f"FROM {_q(table)}"
            ).fetchone()
        except Exception:
            continue
        negative, negative_sum, nulls, total = row
        if negative:
            issues.append(QualityIssue(
                "NEGATIVE_VALUES", "warn", "指标包含负值",
                f"{ref} 有 {negative} 条负值，合计 {float(negative_sum):.2f}；"
                f"可能来自退款/冲正，当前口径未自动排除。",
                {"ref": ref, "negative_rows": int(negative),
                 "negative_sum": float(negative_sum), "rows": int(total)},
                "负值会影响合计/均值，尤其是销售额和金额类指标。",
                "确认业务口径是否应排除负值，或在答案中说明包含负值。",
            ))
        if nulls and total:
            issues.append(QualityIssue(
                "NULL_VALUES", "info", "指标列存在空值",
                f"{ref} 有 {nulls} 个空值，占 {nulls / total:.2%}；"
                f"聚合按可用记录计算。",
                {"ref": ref, "nulls": int(nulls), "rows": int(total)},
                "空值不会进入 SUM/AVG，但可能使合计偏低。",
                "如需完整口径，请确认空值业务含义。",
            ))
    return issues


def scan_quality(plan: QueryPlan, metric, result, sem: SemanticLayer,
                 db_path: Path | str = WAREHOUSE_PATH) -> list[QualityIssue]:
    """Run cheap aggregate checks against the same warehouse."""
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return []
    try:
        issues = []
        policy_issue = _policy_issue(plan, sem)
        if policy_issue:
            issues.append(policy_issue)
        issues.extend(_coverage(plan, metric, sem, con))
        issues.extend(_value_health(metric, sem, con))
        if getattr(result, "meta", {}).get("fanout_used"):
            issues.append(QualityIssue(
                "FANOUT_USED", "info", "查询使用了 1:N 维度",
                "结果经过一对多连接分组，各分组不能直接相加。",
                {"fanout_used": True},
                "同一实体可能进入多个分组，分组值不可简单汇总。",
                "如需总量，请使用整体口径或 DISTINCT 计数。",
            ))
        return issues
    finally:
        con.close()
