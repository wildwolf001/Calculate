"""Business checks that run after execution, before composition.

A check never rewrites the result. It either blocks composition (error) or is
attached to the answer as a verifiable caveat (warn/info).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

import duckdb

from app.config.settings import QUALITY_PATH, WAREHOUSE_PATH
from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan, TimeRange
from app.domain.quality import scan_quality
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
        f"{check.get('title', check.get('id'))}"
        f"（{check.get('count')} "
        f"{'个空值' if check.get('id') == 'missing_values' else '项'}）"
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


REF_RE = re.compile(r"\b(t_[A-Za-z0-9]+)\.([A-Za-z0-9_]+)\b")


def _refs_in(value: object) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return REF_RE.findall(value)
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for item in value.values():
            out.extend(_refs_in(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_refs_in(item))
        return out
    return []


def _plan_metric_refs(plan: QueryPlan, metric, sem: SemanticLayer
                      ) -> list[tuple[str, str]]:
    raw = metric.raw or {}
    refs: list[tuple[str, str]] = []
    for key in (
        "expr", "numerator", "denominator", "grain_numerator",
        "grain_denominator", "operand", "time_via", "entity",
        "count_orders", "default_time_axis",
    ):
        refs.extend(_refs_in(raw.get(key)))
    refs.extend(_refs_in(list((raw.get("scope") or {}).keys())))
    if plan.time_axis:
        refs.extend(_refs_in(plan.time_axis))
    # Deliberately do NOT include grouping dimensions here: a null in a
    # group-by column is a separate, filter-dependent question. The quality
    # notice should only claim what the metric actually reads.
    out: list[tuple[str, str]] = []
    for ref in refs:
        if ref not in out:
            out.append(ref)
    return out


def _quality_missing(quality: dict | None) -> dict[tuple[str, str], int]:
    detail: dict = {}
    for check in (quality or {}).get("checks") or []:
        if check.get("id") == "missing_values":
            detail = check.get("detail") or {}
            break
    out: dict[tuple[str, str], int] = {}
    if isinstance(detail, dict):
        for table, columns in detail.items():
            if not isinstance(columns, dict):
                continue
            for column, count in columns.items():
                try:
                    out[(str(table), str(column))] = int(count)
                except (TypeError, ValueError):
                    continue
    return out


def _field_label(ref: tuple[str, str], metric, sem: SemanticLayer) -> str:
    table, column = ref
    for dim in sem.dimensions.values():
        raw = dim.raw
        if str(raw.get("column") or "") == f"{table}.{column}":
            return str(raw.get("name_zh") or dim.id)
        if f"{table}.{column}" in str(raw.get("expr") or ""):
            return str(raw.get("name_zh") or dim.id)
    for axis in sem.time_axes or []:
        if str(axis.get("ref") or "") == f"{table}.{column}":
            return str(axis.get("name_zh") or column)
    if metric is not None and f"{table}.{column}" in str((metric.raw or {}).get("expr") or ""):
        return str(metric.name)
    return column


def _filtered_null_count(db_path, table: str, column: str, plan: QueryPlan,
                         metric, sem: SemanticLayer) -> int | None:
    """Count nulls after the plan's filters/time, when a safe probe is possible.

    The catalog count is table-wide. For a query filtered to delivered orders,
    a table-wide 21 is misleading if all 21 belong to non-delivered rows. We
    only probe when every filter/time predicate lives on the same table and is
    a simple column predicate; otherwise return None and keep the table-wide
    wording.
    """
    if not db_path:
        return None
    predicates = [f'"{column}" IS NULL']
    params: list = []
    for dim_id, spec in plan.filters.items():
        dim = sem.dimensions.get(dim_id)
        if dim is None:
            return None
        ref = str(dim.raw.get("filter_column") or dim.raw.get("column") or "")
        if "." not in ref:
            return None
        ref_table, ref_column = ref.split(".", 1)
        if ref_table != table:
            return None
        if isinstance(spec, dict):
            op = str(spec.get("op") or "eq")
            value = spec.get("value")
        else:
            op = str(getattr(spec, "op", "eq") or "eq")
            value = getattr(spec, "value", None)
        if op == "eq":
            predicates.append(f'"{ref_column}" = ?')
            params.append(value)
        elif op == "in" and isinstance(value, (list, tuple)):
            if not value:
                return None
            predicates.append(
                f'"{ref_column}" IN ({", ".join("?" for _ in value)})'
            )
            params.extend(value)
        else:
            return None
    # Include the metric's own scope (e.g. delivered) even if the controller
    # did not materialize it as a plan filter.
    for scope_ref, scope_value in ((metric.raw or {}).get("scope") or {}).items():
        if "." not in str(scope_ref):
            continue
        scope_table, scope_column = str(scope_ref).split(".", 1)
        if scope_table != table or isinstance(scope_value, (list, tuple, dict)):
            continue
        predicates.append(f'"{scope_column}" = ?')
        params.append(scope_value)
    if plan.time is not None:
        time_ref = str((metric.raw or {}).get("time_via") or "")
        if time_ref and "." in time_ref:
            time_table, time_column = time_ref.split(".", 1)
        else:
            time_table = table
            time_column = time_ref or str(
                ((sem.tables.get(table) or {}).get("time") or "")
            )
        if time_table != table or not time_column:
            return None
        predicates.append(f'"{time_column}" >= ?')
        predicates.append(f'"{time_column}" < ?')
        params.extend([str(plan.time.start), str(plan.time.end)])
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            row = con.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE ' + " AND ".join(predicates),
                params,
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    return int(row[0]) if row else None


def _relevant_quality_notice(plan: QueryPlan, metric, sem: SemanticLayer,
                             quality: dict | None,
                             catalog: dict | None,
                             db_path=None,
                             ) -> tuple[CheckIssue | None, list[dict]]:
    """Only report nulls that the current query actually reads."""
    if not catalog:
        return _quality_notice(quality)
    missing = _quality_missing(quality)
    refs = _plan_metric_refs(plan, metric, sem)
    columns = {
        (table_id, str(col.get("name"))): col
        for table_id, table in ((catalog.get("tables") or {})).items()
        for col in table.get("columns") or []
    }
    relevant: list[tuple[tuple[str, str], int, str]] = []
    for ref in refs:
        col = columns.get(ref)
        count = None
        if col is not None:
            try:
                count = int(col.get("nulls") or 0)
            except (TypeError, ValueError):
                count = None
        if count is None:
            count = missing.get(ref)
        if not count:
            continue
        filtered = _filtered_null_count(
            db_path, ref[0], ref[1], plan, metric, sem,
        )
        if filtered is not None:
            count = filtered
        if count:
            relevant.append((ref, count, _field_label(ref, metric, sem)))
    if not relevant:
        return None, []

    parts = [
        f"{label} {count} 条空值"
        for (_table, _column), count, label in relevant[:3]
    ]
    dim_refs = {
        ref
        for dim_id in list(plan.dimensions) + list(plan.filters)
        for dim in [sem.dimensions.get(dim_id)]
        if dim is not None
        for ref in _refs_in(dim.raw.get("column"))
        + _refs_in(dim.raw.get("expr"))
    }
    derived_hit = None
    for dim_id in list(plan.dimensions) + list(plan.filters):
        dim = sem.dimensions.get(dim_id)
        if dim is None or str(dim.raw.get("kind") or "") != "derived":
            continue
        dim_refs_in_expr = set(_refs_in(dim.raw.get("expr")))
        if any(ref in dim_refs_in_expr for ref, _count, _label in relevant):
            labels = dim.raw.get("labels") or {}
            derived_hit = (
                str(dim.raw.get("name_zh") or dim_id),
                str(labels.get("true") or "命中"),
                str(labels.get("false") or "未命中"),
            )
            break
    if derived_hit:
        dim_name, true_label, false_label = derived_hit
        impact = (
            f"「{dim_name}」的条件判定依赖这些字段；空值不会被判定为"
            f"「{true_label}」或「{false_label}」，会进入空分组或被排除，"
            "相关比例/分组值可能偏差。"
        )
    elif any(ref in dim_refs for ref, _count, _label in relevant):
        impact = "这些记录无法进入对应分组或无法判定条件，相关比例/分组值可能偏差"
    elif any(
        ref == tuple(str((metric.raw or {}).get("time_via") or "").split(".", 1))
        for ref, _count, _label in relevant
    ):
        impact = "这些记录不会进入按时间筛选的结果"
    else:
        impact = "聚合只按非空记录计算，缺失记录会被排除"
    detail = (
        f"本次查询实际使用的字段中：{'、'.join(parts)}。"
        f"{impact.rstrip('。')}。未涉及的字段缺失不影响该结果。"
    )
    return (
        CheckIssue("DATA_QUALITY", "warn", "相关列缺失", detail),
        [
            {
                "field": f"{table}.{column}",
                "label": label,
                "count": count,
            }
            for (table, column), count, label in relevant[:8]
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
    if plan.degraded_reason:
        facts["degraded_reason"] = plan.degraded_reason
        issues.append(CheckIssue(
            "SCOPE_TIME_DROPPED", "warn", "时间条件未应用",
            plan.degraded_reason,
        ))
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
    if plan.time and plan.filters:
        scope += "筛选条件与时间窗已应用到成员清单。"
    elif plan.time:
        scope += "时间窗已应用到成员清单。"
    elif plan.filters:
        scope += "筛选条件已应用到成员清单；时间条件未应用。"
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
               db_path=WAREHOUSE_PATH, quality: dict | None = None,
               catalog: dict | None = None) -> tuple[list[CheckIssue], dict]:
    """Return (issues, facts). `facts` is machine-readable context for the composer."""
    issues: list[CheckIssue] = []
    facts: dict = {
        "metric": plan.metric,
        "dimensions": plan.dimensions,
        "fanout_used": bool(result.meta.get("fanout_used")),
    }
    if plan.degraded_reason:
        facts["degraded_reason"] = plan.degraded_reason
        if plan.degraded_kind == "time":
            issues.append(CheckIssue(
                "TIME_CLAMPED", "info", "时间范围已裁剪", plan.degraded_reason,
            ))
        else:
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

    if "share" in result.columns and metric.raw.get("additive"):
        share_idx = result.columns.index("share")
        share_values = [
            float(row[share_idx]) for row in result.rows
            if row[share_idx] is not None
        ]
        if share_values:
            total_share = sum(share_values)
            facts["share_sum"] = round(total_share, 6)
            if not result.truncated and abs(total_share - 1.0) > SHARE_TOLERANCE:
                issues.append(CheckIssue(
                    "SHARE_SUM_MISMATCH", "warn", "占比合计不为 1",
                    f"当前分组占比合计为 {total_share:.4f}，"
                    "可能因为结果被截断或存在未归组记录。",
                ))

    # ---- additive share check ----
    additive_metric = bool(metric.raw.get("additive"))
    can_validate_share = additive_metric and not plan.filters
    additive = (
        can_validate_share
        and not (plan.compare or plan.comparison)
        and not plan.limit_per_group
    )
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

    # ---- period comparison ----
    ranges = _comparison_ranges(plan, result)
    delta_desc_top_n = bool(plan.order_by) and (
        plan.order_by[0].field == "delta"
        and plan.order_by[0].direction == "desc"
    )
    ranked_top_n = intentional_top_n and delta_desc_top_n
    if ranges and additive_metric and plan.dimensions and (
        not possibly_truncated or ranked_top_n
    ):
        current_range, baseline_range = ranges
        try:
            ci = result.columns.index("current")
            bi = result.columns.index("baseline")
            di = result.columns.index("delta")
            gi = (
                result.columns.index("growth_rate")
                if "growth_rate" in result.columns else None
            )
            current_sum = sum(
                float(row[ci]) for row in result.rows if row[ci] is not None
            )
            baseline_sum = sum(
                float(row[bi]) for row in result.rows if row[bi] is not None
            )
            delta_sum = sum(
                float(row[di]) for row in result.rows if row[di] is not None
            )
            current_total = baseline_total = None
            if can_validate_share:
                current_total = _metric_total(
                    plan, metric, sem, db_path, time=current_range
                )
                baseline_total = _metric_total(
                    plan, metric, sem, db_path, time=baseline_range
                )
                facts["comparison_current_total"] = current_total
                facts["comparison_baseline_total"] = baseline_total
                if current_total:
                    facts["current_share_of_total"] = round(
                        current_sum / current_total, 4
                    )
                if baseline_total:
                    facts["baseline_share_of_total"] = round(
                        baseline_sum / baseline_total, 4
                    )
                total_delta = current_total - baseline_total
            elif not possibly_truncated:
                total_delta = delta_sum
            else:
                total_delta = None
            if total_delta is not None:
                facts["comparison_delta_total"] = total_delta

            dims = plan.dimensions
            dimension_names = [
                sem.dimension(dim).raw.get("name_zh", dim) for dim in dims
            ]

            def member_label(row: list) -> str:
                return " / ".join(str(value) for value in row[:len(dims)])

            positive = [
                row for row in result.rows
                if row[di] is not None and float(row[di]) > 0
            ]
            negative = [
                row for row in result.rows
                if row[di] is not None and float(row[di]) < 0
            ]
            if positive:
                top = max(positive, key=lambda row: float(row[di]))
                facts["top_delta_member"] = {
                    "dimension_id": dims[0] if len(dims) == 1 else dims,
                    "dimension_name": (
                        dimension_names[0]
                        if len(dimension_names) == 1 else dimension_names
                    ),
                    "value": member_label(top),
                    "delta": float(top[di]),
                    "growth_rate": (
                        float(top[gi]) if gi is not None and top[gi] is not None
                        else None
                    ),
                }
            if negative and not possibly_truncated:
                worst = min(negative, key=lambda row: float(row[di]))
                facts["largest_negative_contributor"] = {
                    "dimension_id": dims[0] if len(dims) == 1 else dims,
                    "dimension_name": (
                        dimension_names[0]
                        if len(dimension_names) == 1 else dimension_names
                    ),
                    "value": member_label(worst),
                    "delta": float(worst[di]),
                    "growth_rate": (
                        float(worst[gi]) if gi is not None and worst[gi] is not None
                        else None
                    ),
                }
                facts["negative_contributors"] = [
                    {
                        "value": member_label(row),
                        "delta": float(row[di]),
                    }
                    for row in sorted(negative, key=lambda row: float(row[di]))
                ]
            if (
                positive
                and total_delta is not None
                and abs(total_delta) > 1e-9
            ):
                top_delta = facts["top_delta_member"]["delta"]
                facts["top_contribution_share"] = round(
                    top_delta / total_delta, 4
                )

            if can_validate_share and not possibly_truncated:
                for label, actual, expected in (
                    ("本期", current_sum, current_total),
                    ("基期", baseline_sum, baseline_total),
                ):
                    if not expected:
                        continue
                    share = actual / expected
                    if abs(share - 1.0) > SHARE_TOLERANCE:
                        issues.append(CheckIssue(
                            "COMPARISON_SHARE_MISMATCH",
                            "error",
                            f"{label}分组合计与总体不一致",
                            f"分组合计 {actual:.2f}，总体 {expected:.2f}，"
                            f"份额 {share:.2%}；可能是粒度或 join 放大导致。",
                        ))
        except (ValueError, ZeroDivisionError, duckdb.Error) as exc:
            issues.append(CheckIssue(
                "COMPARISON_CHECK_FAILED", "warn", "比较口径校验未完成", str(exc)
            ))

    if plan.compare or plan.comparison:
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
    quality_issue, quality_facts = _relevant_quality_notice(
        plan, metric, sem, q, catalog, db_path=db_path,
    )
    if quality_issue:
        issues.append(quality_issue)
        facts["data_quality"] = quality_facts
    # ---- metric-level caveats ----
    for caveat in metric.raw.get("caveats", []) or []:
        issues.append(CheckIssue("METRIC_CAVEAT", "info", "口径提示", caveat))
    if metric.raw.get("coverage_note"):
        issues.append(CheckIssue("COVERAGE", "info", "覆盖范围",
                                 metric.raw["coverage_note"]))

    health = metric.raw.get("data_health") or {}
    duplicate_ratio = health.get("duplicate_key_ratio")
    expression = str(
        metric.raw.get("expr") or metric.raw.get("numerator") or ""
    ).upper()
    if (
        isinstance(duplicate_ratio, (int, float))
        and duplicate_ratio > 1.05
        and "AVG(" in expression
    ):
        facts["avg_grain_ratio"] = duplicate_ratio
        issues.append(CheckIssue(
            "AVG_GRAIN", "warn", "平均值按记录加权",
            f"同一订单平均有 {duplicate_ratio:.2f} 条记录，"
            "AVG 会按记录数加权，不是按订单平均。",
        ))

    if metric.type == "ratio":
        facts["ratio_grain"] = plan.ratio_grain or metric.raw.get("ratio_grain")
        issues.append(CheckIssue(
            "RATIO_GRAIN", "info", "比率粒度",
            f"比率已先聚合到 {plan.ratio_grain or metric.raw.get('ratio_grain')} 粒度再相除，"
            f"不是对明细行求平均。",
        ))
        if facts.get("fanout_used") and plan.dimensions:
            issues.append(CheckIssue(
                "RATIO_FANOUT", "warn", "比率分组存在重复分母",
                "该比率按一对多维度分组，同一实体可能进入多个组；"
                "各组比率不能简单加总。",
            ))

    # ---- multi-valued dimension (generic) ----
    expression = str(metric.raw.get("expr") or "").upper()
    if (
        facts.get("fanout_used")
        and plan.dimensions
        and "DISTINCT" in expression
    ):
        issues.append(CheckIssue(
            "MULTI_VALUED_DIMENSION", "warn", "维度可多值",
            "同一实体可能属于多个分组，分组计数之和可能大于总体；"
            "这是多值维度的正常结果。",
        ))

    # Query-aware checks: missing months, partial periods, negative values.
    try:
        scan = scan_quality(plan, metric, result, sem, db_path=db_path)
        for item in scan:
            issues.append(CheckIssue(
                item.code, item.severity, item.title, item.detail,
            ))
            facts.setdefault("quality_scan", []).append({
                "code": item.code,
                "severity": item.severity,
                "evidence": item.evidence,
                "impact": item.impact,
                "suggestion": item.suggestion,
            })
    except Exception as exc:
        issues.append(CheckIssue(
            "QUALITY_SCAN_FAILED", "info", "数据质量扫描未完成", str(exc),
        ))

    return issues, facts


def _comparison_ranges(plan: QueryPlan, result: ResultSet):
    if plan.comparison:
        return plan.comparison.current, plan.comparison.baseline
    current = result.meta.get("current_range")
    baseline = result.meta.get("baseline_range")
    if not current or not baseline:
        return None
    try:
        if plan.time is None:
            return None
        return (
            TimeRange(start=date.fromisoformat(current[0]),
                      end=date.fromisoformat(current[1])),
            TimeRange(start=date.fromisoformat(baseline[0]),
                      end=date.fromisoformat(baseline[1])),
        )
    except (TypeError, ValueError, IndexError):
        return None


def _metric_total(plan: QueryPlan, metric, sem: SemanticLayer, db_path,
                  time=None) -> float:
    """Total for additive share checks: same metric/scope/time, no dimensions."""
    from app.domain.compiler import compile_plan

    total_plan = plan.model_copy(deep=True)
    total_plan.dimensions = []
    total_plan.filters = {}
    total_plan.order_by = []
    total_plan.having = []
    total_plan.limit = 1
    total_plan.compare = None
    total_plan.comparison = None
    if time is not None:
        total_plan.time = time
    compiled = compile_plan(total_plan, sem)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(compiled.sql, compiled.params).fetchone()
    finally:
        con.close()
    if not row or row[0] is None:
        return 0.0
    return float(row[0])

