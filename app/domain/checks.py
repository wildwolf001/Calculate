"""Business checks that run after execution, before composition.

A check never rewrites the result. It either blocks composition (error) or is
attached to the answer as a verifiable caveat (warn/info).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import duckdb

from app.config.settings import QUALITY_PATH, WAREHOUSE_PATH
from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import SemanticLayer

SHARE_TOLERANCE = 0.005  # 0.5%


@dataclass
class CheckIssue:
    id: str
    severity: str      # error | warn | info
    title: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "title": self.title, "detail": self.detail}


def _quality_counts(path=QUALITY_PATH) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["id"]: c for c in data.get("checks", [])}


def _quality_notice(quality: dict | None) -> tuple[CheckIssue | None, list[dict]]:
    checks = (quality or {}).get("checks") or []
    notable = [
        check for check in checks
        if check.get("severity") in {"error", "warn"}
        and (check.get("count") or 0) > 0
    ]
    if not notable:
        return None, []
    summary = "；".join(
        f"{check.get('title', check.get('id'))}（{check.get('count')}）"
        for check in notable[:4]
    )
    return (
        CheckIssue(
            "DATA_QUALITY",
            "warn",
            "数据质量提示",
            f"{summary}。本次结果按可用记录计算，缺失或异常数据可能影响结论。",
        ),
        [
            {
                "id": check.get("id"),
                "severity": check.get("severity"),
                "title": check.get("title"),
                "count": check.get("count"),
            }
            for check in notable[:8]
        ],
    )


def run_member_list_checks(plan: QueryPlan, result: ResultSet,
                           quality: dict | None = None
                           ) -> tuple[list[CheckIssue], dict]:
    """Checks for the member_list shape (no metric involved)."""
    issues: list[CheckIssue] = []
    facts: dict = {
        "lookup": plan.list_dim,
        "row_count": result.row_count,
        "filters": list(plan.filters),
        "range": [str(plan.time.start), str(plan.time.end)] if plan.time else None,
    }
    if result.row_count == 0:
        issues.append(CheckIssue("EMPTY_RESULT", "warn", "清单为空",
                                 "当前筛选与时间范围内没有成员；可能是筛选条件、时间范围或源数据缺失。"))
        facts["empty_result"] = True
        quality_issue, quality_facts = _quality_notice(quality)
        if quality_issue:
            issues.append(quality_issue)
            facts["data_quality"] = quality_facts
        return issues, facts
    if result.truncated or result.row_count >= plan.limit:
        issues.append(CheckIssue("TRUNCATED", "warn", "结果被截断",
                                 f"仅返回前 {result.row_count} 行成员。"))
    scope = "清单来自维度表。"
    if plan.time or plan.filters:
        scope += "筛选条件与时间窗已应用到成员清单。"
    issues.append(CheckIssue("LOOKUP_SCOPE", "info", "统计范围", scope))
    quality_issue, quality_facts = _quality_notice(quality)
    if quality_issue:
        issues.append(quality_issue)
        facts["data_quality"] = quality_facts
    return issues, facts


def run_coverage_checks(plan: QueryPlan, metric, result: ResultSet,
                        quality: dict | None = None
                        ) -> tuple[list[CheckIssue], dict]:
    issues: list[CheckIssue] = []
    expected: list[str] = []
    observed: list[str] = []
    missing: list[str] = []
    zero_value: list[str] = []
    values: list[dict] = []
    columns = result.columns
    month_idx = columns.index("month") if "month" in columns else 0
    count_idx = columns.index("record_count") if "record_count" in columns else None
    value_idx = columns.index("value") if "value" in columns else None
    missing_idx = columns.index("is_missing") if "is_missing" in columns else None
    for row in result.rows:
        month = str(row[month_idx])
        expected.append(month)
        is_missing = bool(row[missing_idx]) if missing_idx is not None else False
        record_count = row[count_idx] if count_idx is not None else None
        metric_value = row[value_idx] if value_idx is not None else None
        if is_missing or record_count is None:
            missing.append(month)
        else:
            observed.append(month)
            if metric_value is not None and float(metric_value) == 0:
                zero_value.append(month)
        values.append({
            "month": month,
            "record_count": record_count,
            "value": metric_value,
            "is_missing": is_missing or record_count is None,
        })
    total = len(expected)
    facts = {
        "coverage_period": plan.coverage_period,
        "coverage_range": [
            str(plan.time.start), str(plan.time.end)
        ] if plan.time else None,
        "expected_periods": expected,
        "observed_periods": observed,
        "missing_periods": missing,
        "zero_value_periods": zero_value,
        "coverage_ratio": round(len(observed) / total, 4) if total else 0.0,
        "coverage_metric": metric.id,
        "coverage_rows": values,
    }
    if total == 0:
        issues.append(CheckIssue(
            "COVERAGE_EMPTY_RANGE", "warn", "覆盖区间为空",
            "指定时间范围内没有可检查的自然月。",
        ))
    elif missing:
        issues.append(CheckIssue(
            "TIME_GAP", "warn", "存在缺失时间段",
            f"应覆盖 {total} 个月，实际有记录 {len(observed)} 个月；"
            f"缺失：{'、'.join(missing)}。",
        ))
    else:
        issues.append(CheckIssue(
            "COVERAGE_COMPLETE", "info", "时间覆盖完整",
            f"{total} 个自然月均有记录。",
        ))
    if zero_value:
        issues.append(CheckIssue(
            "ZERO_VALUE_PERIOD", "warn", "存在零业务量月份",
            f"以下月份有记录但指标值为 0：{'、'.join(zero_value)}。",
        ))
    quality_issue, quality_facts = _quality_notice(quality)
    if quality_issue:
        issues.append(quality_issue)
        facts["data_quality"] = quality_facts
    return issues, facts


def run_checks(plan: QueryPlan, metric, result: ResultSet, sem: SemanticLayer,
               db_path=WAREHOUSE_PATH, quality: dict | None = None) -> tuple[list[CheckIssue], dict]:
    """Return (issues, facts). `facts` is machine-readable context for the composer."""
    issues: list[CheckIssue] = []
    facts: dict = {"metric": plan.metric, "dimensions": plan.dimensions}
    if plan.degraded_reason:
        facts["degraded_reason"] = plan.degraded_reason
        issues.append(CheckIssue(
            "DEGRADED_DIMENSION", "warn", "已退化为整体口径", plan.degraded_reason,
        ))

    if result.row_count == 0:
        issues.append(CheckIssue("EMPTY_RESULT", "warn", "查询结果为空",
                                 "当前口径、筛选或时间范围内没有可用数据；"
                                 "可能是数据缺失、筛选无匹配或源数据未覆盖。"))
        facts["empty_result"] = True
        quality_issue, quality_facts = _quality_notice(quality)
        if quality_issue:
            issues.append(quality_issue)
            facts["data_quality"] = quality_facts
        return issues, facts

    if result.truncated:
        issues.append(CheckIssue("TRUNCATED", "warn", "结果被截断",
                                 f"仅返回前 {result.row_count} 行，排名/份额判断需谨慎。"))

    # A result that exactly hits the plan limit may have more rows behind it;
    # share checks need the complete dimension set.
    possibly_truncated = result.truncated or result.row_count >= plan.limit
    intentional_top_n = bool(plan.order_by) and plan.limit <= 20
    if possibly_truncated and not result.truncated and plan.dimensions and not intentional_top_n:
        issues.append(CheckIssue("AT_LIMIT", "warn", "结果达到行数上限",
                                 f"返回 {result.row_count} 行，等于计划的 limit={plan.limit}；"
                                 f"可能还有更多分组。"))

    # ---- additive share check ----
    additive = bool(metric.raw.get("additive")) and not plan.filters and not plan.compare
    if additive and plan.dimensions and not possibly_truncated:
        try:
            idx = result.columns.index("value")
            vals = [r[idx] for r in result.rows if r[idx] is not None]
            if any(v is None for r in result.rows for v in [r[idx]]):
                issues.append(CheckIssue("NULL_DIM_MEMBER", "warn", "存在空维度值",
                                         "聚合结果包含空值成员，展示时已单列。"))
            total = sum(float(v) for v in vals)
            if total > 0:
                share = total / _metric_total(plan, metric, sem, db_path)
                facts["share_of_total"] = round(share, 4)
                if abs(share - 1.0) > SHARE_TOLERANCE:
                    issues.append(CheckIssue(
                        "SHARE_MISMATCH", "error", "分项合计与总体不一致",
                        f"分项合计 {total:.2f}，总体 {_metric_total(plan, metric, sem, db_path):.2f}，"
                        f"份额 {share:.2%}；可能是粒度或 join 放大导致。",
                    ))
        except (ValueError, ZeroDivisionError, duckdb.Error) as exc:
            issues.append(CheckIssue("SHARE_CHECK_FAILED", "warn", "份额校验未完成", str(exc)))

    # ---- compare baseline ----
    if plan.compare:
        baseline_idx = result.columns.index("baseline") if "baseline" in result.columns else None
        if baseline_idx is not None:
            baselines = [r[baseline_idx] for r in result.rows]
            facts["baseline_range"] = result.meta.get("baseline_range")
            facts["current_range"] = result.meta.get("current_range")
            if any(v is None for v in baselines):
                issues.append(CheckIssue("BASELINE_MISSING", "warn", "基期缺失",
                                         "部分分组在上期没有数据，增长率显示为空。"))
            elif any(float(v) == 0 for v in baselines):
                issues.append(CheckIssue("BASELINE_ZERO", "warn", "基期为 0",
                                         "基期为 0，无法计算增长率（已用 NULL 而非无穷大）。"))

    q = quality if quality is not None else _quality_counts()
    quality_issue, quality_facts = _quality_notice(q)
    if quality_issue:
        issues.append(quality_issue)
        facts["data_quality"] = quality_facts
    business_checks = set(sem.capabilities.get("business_checks") or [])

    # ---- metric-level caveats ----
    for caveat in metric.raw.get("caveats", []) or []:
        issues.append(CheckIssue("METRIC_CAVEAT", "info", "口径提示", caveat))
    if metric.raw.get("coverage_note"):
        issues.append(CheckIssue("COVERAGE", "info", "覆盖范围",
                                 metric.raw["coverage_note"]))

    if metric.type == "ratio":
        facts["ratio_grain"] = plan.ratio_grain or metric.raw.get("ratio_grain")
        issues.append(CheckIssue(
            "RATIO_GRAIN", "info", "比率粒度",
            f"比率已先聚合到 {plan.ratio_grain or metric.raw.get('ratio_grain')} 粒度再相除，"
            f"不是对明细行求平均。",
        ))
        if "category" in plan.dimensions and q.get("multi_category_orders"):
            issues.append(CheckIssue(
                "RATIO_CROSS_CATEGORY", "warn", "跨品类订单在分组内重复计入分母",
                f"有 {q['multi_category_orders']['count']} 笔订单跨多个品类；"
                f"分组比率之和与整体比率不相等，这是分组口径的正常结果。",
            ))
        if "seller" in plan.dimensions and q.get("multi_seller_orders"):
            issues.append(CheckIssue(
                "RATIO_CROSS_SELLER", "warn", "跨商家订单在分组内重复计入分母",
                f"有 {q['multi_seller_orders']['count']} 笔订单跨多个商家；"
                f"各商家比率不能简单加总。",
            ))

    # ---- multi-* order duplication ----
    if plan.metric == "order_count":
        if "category" in plan.dimensions and q.get("multi_category_orders"):
            n = q["multi_category_orders"]["count"]
            issues.append(CheckIssue(
                "CROSS_CATEGORY_ORDER", "warn", "跨品类订单会重复计数",
                f"有 {n} 笔订单包含多个品类，按品类统计订单数时会被计入多次；"
                f"各品类订单数之和会大于总订单数。",
            ))
        if "seller" in plan.dimensions and q.get("multi_seller_orders"):
            n = q["multi_seller_orders"]["count"]
            issues.append(CheckIssue(
                "CROSS_SELLER_ORDER", "warn", "跨商家订单会重复计数",
                f"有 {n} 笔订单包含多个商家，按商家统计订单数时会被计入多次。",
            ))

    if ("split_payment" in business_checks
            and plan.metric == "paid_amount" and "payment_type" in plan.dimensions):
        n = q.get("multi_payment_orders", {}).get("count", 0)
        issues.append(CheckIssue(
            "SPLIT_PAYMENT", "info", "拆分支付",
            f"有 {n} 笔订单使用多笔支付，按支付方式拆分时同一订单可能出现在多行；"
            f"支付金额按支付记录归属，合计仍等于总额。",
        ))

    # ---- payment reconciliation (only for paid_amount) ----
    if plan.metric == "paid_amount" and "payment_reconciliation" in business_checks:
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                diff = con.execute("""
                    WITH items AS (
                      SELECT i.order_id, SUM(i.price) + SUM(i.freight_value) AS expected
                      FROM order_items i JOIN orders o USING(order_id)
                      WHERE o.order_status='delivered' GROUP BY 1
                    ), pays AS (
                      SELECT p.order_id, SUM(p.payment_value) AS paid
                      FROM order_payments p JOIN orders o USING(order_id)
                      WHERE o.order_status='delivered' GROUP BY 1
                    )
                    SELECT COUNT(*) FROM items i JOIN pays p USING(order_id)
                    WHERE ABS(i.expected - p.paid) > 0.01
                """).fetchone()[0]
            finally:
                con.close()
            facts["payment_mismatch_orders"] = int(diff)
            issues.append(CheckIssue(
                "PAYMENT_RECONCILIATION", "info" if diff == 0 else "warn", "支付勾稽",
                f"已送达订单中，支付金额与商品金额+运费差异超过 0.01 元的订单数：{diff}。",
            ))
        except duckdb.Error as exc:
            issues.append(CheckIssue("PAYMENT_CHECK_FAILED", "warn", "支付勾稽未完成", str(exc)))

    return issues, facts


def _metric_total(plan: QueryPlan, metric, sem: SemanticLayer, db_path) -> float:
    """Total for additive share checks: same metric/scope/time, no dimensions."""
    from app.domain.compiler import compile_plan

    total_plan = plan.model_copy(deep=True)
    total_plan.dimensions = []
    total_plan.filters = {}
    total_plan.order_by = []
    total_plan.having = []
    total_plan.limit = 1
    total_plan.compare = None
    compiled = compile_plan(total_plan, sem)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(compiled.sql, compiled.params).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return 0.0
    return float(row[0])
