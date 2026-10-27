"""Dataset readiness probes.

A dataset is "ready" only when the selected semantic layer can actually
answer representative questions. Checks are deterministic and run against
the dataset's own warehouse.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.datasets.context import DatasetContext
from app.domain.pipeline import run_query
from app.domain.plan import QueryPlan, TimeRange


def _time_range(ctx: DatasetContext) -> TimeRange | None:
    try:
        start, end = ctx.sem.time_range
        return TimeRange(start=start, end=end)
    except Exception:
        return None


def _probe(ctx: DatasetContext, metric_id: str, dimensions: list[str] | None = None,
           time: TimeRange | None = None, label: str = "overall") -> dict[str, Any]:
    plan = QueryPlan(
        metric=metric_id,
        dimensions=dimensions or [],
        time=time,
        limit=10,
    )
    try:
        outcome = run_query(plan, ctx.sem, db_path=ctx.warehouse, quality=ctx.quality)
    except Exception as exc:
        return {
            "id": f"metric:{metric_id}:{label}",
            "status": "error",
            "metric": metric_id,
            "label": label,
            "detail": f"{type(exc).__name__}: {exc}"[:300],
        }
    if not outcome.ok:
        return {
            "id": f"metric:{metric_id}:{label}",
            "status": "error",
            "metric": metric_id,
            "label": label,
            "detail": (outcome.error or "查询失败")[:300],
        }
    rows = outcome.result.rows if outcome.result else []
    if not rows:
        return {
            "id": f"metric:{metric_id}:{label}",
            "status": "warn",
            "metric": metric_id,
            "label": label,
            "detail": "探测结果为空",
        }
    return {
        "id": f"metric:{metric_id}:{label}",
        "status": "pass",
        "metric": metric_id,
        "label": label,
        "row_count": len(rows),
    }


def build_readiness(ctx: DatasetContext,
                    metric_ids: list[str] | None = None,
                    dimension_ids: list[str] | None = None) -> dict[str, Any]:
    sem = ctx.sem
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    if not (ctx.catalog.get("tables") or {}):
        blockers.append("数据源没有可用的表画像")
    checks.append({
        "id": "tables_present",
        "status": "error" if not (ctx.catalog.get("tables") or {}) else "pass",
        "detail": f"{len(ctx.catalog.get('tables') or {})} 张表",
    })
    if not ctx.warehouse.exists():
        blockers.append("仓库文件不存在")
    checks.append({
        "id": "warehouse_present",
        "status": "pass" if ctx.warehouse.exists() else "error",
        "detail": str(ctx.warehouse.name),
    })

    metrics = [
        m for m in sem.metrics.values()
        if metric_ids is None or m.id in metric_ids
    ]
    if metric_ids is not None:
        missing = [mid for mid in metric_ids if mid not in sem.metrics]
        for mid in missing:
            blockers.append(f"未知指标: {mid}")
    if not metrics:
        blockers.append("没有选中任何指标")

    facts = sorted({m.fact for m in metrics if m.raw.get("fact") in sem.tables})
    checks.append({
        "id": "primary_fact",
        "status": "pass" if facts else "error",
        "detail": facts,
    })
    if not facts:
        blockers.append("没有可用的指标事实表")

    bad_joins = []
    for child, links in (sem.raw.get("joins") or {}).items():
        for parent, spec in links.items():
            if spec.get("card") not in {"N:1", "1:1"}:
                bad_joins.append(f"{child}->{parent}")
    checks.append({
        "id": "join_cardinality",
        "status": "error" if bad_joins else "pass",
        "detail": bad_joins or "all N:1/1:1",
    })
    if bad_joins:
        blockers.append("存在非 N:1 关系: " + ", ".join(bad_joins))

    time_range = _time_range(ctx)
    has_time = any(m.raw.get("time_via") for m in metrics)
    if not time_range:
        warnings.append("语义层时间范围不可用")
    elif not has_time:
        warnings.append("选中指标没有时间列，时间趋势类问题不可用")

    dimension_cap = [
        d.id for d in sem.dimensions.values()
        if dimension_ids is None or d.id in dimension_ids
    ]
    checks.append({
        "id": "dimensions_available",
        "status": "pass" if dimension_cap else "warn",
        "detail": len(dimension_cap),
    })
    if not dimension_cap:
        warnings.append("没有可用的维度，只能回答整体指标")

    probe_count = 0
    for metric in metrics[:40]:
        metric_time = time_range if metric.raw.get("time_via") else None
        checks.append(_probe(ctx, metric.id, [], metric_time, "overall"))
        probe_count += 1
        supported = [
            g for g in metric.raw.get("supported_grain") or []
            if g not in {"none", "month"} and g in sem.dimensions
        ]
        if supported:
            checks.append(_probe(ctx, metric.id, [supported[0]], metric_time,
                                 f"by:{supported[0]}"))
            probe_count += 1
        if "month" in (metric.raw.get("supported_grain") or []) and metric.raw.get("time_via"):
            checks.append(_probe(ctx, metric.id, ["month"], metric_time, "by:month"))
            probe_count += 1

    for check in checks:
        if check.get("status") == "error":
            detail = check.get("detail") or check.get("id")
            blockers.append(f"{check.get('id')}: {detail}")
        elif check.get("status") == "warn":
            warnings.append(f"{check.get('id')}: {check.get('detail', '')}".strip())

    # Keep blocker text readable and unique.
    blockers = list(dict.fromkeys(blockers))
    warnings = list(dict.fromkeys(warnings))
    status = "blocked" if blockers else "ready"
    return {
        "status": status,
        "dataset_id": ctx.dataset_id,
        "semantic_version": ctx.semantic_version,
        "data_version": ctx.data_version,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "probes": probe_count,
        "metrics": [m.id for m in metrics],
        "dimensions": dimension_cap,
        "capabilities": ctx.capabilities,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_readiness(root: Path, readiness: dict[str, Any]) -> Path:
    path = root / "readiness.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(readiness, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
