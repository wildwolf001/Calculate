"""Dataset-aware plans for the on-demand business analysis report.

The report is deliberately generated from the current semantic layer rather
than from a fixed e-commerce schema. A module only appears when its metric and
dimension combination is actually supported by the selected dataset.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.plan import ComparisonSpec, TimeRange
from app.domain.semantic.loader import Metric, SemanticLayer


@dataclass
class AnalysisItem:
    id: str
    section: str
    title: str
    slots: dict[str, Any]
    module: str
    apply_filters: bool = True


MODULE_META = {
    "overview": ("经营概览", "规模与趋势", "核心指标总量与覆盖范围"),
    "trend": ("趋势", "规模与趋势", "近 12 个月核心指标走势"),
    "structure": ("结构", "结构与增长", "按主要业务维度拆分核心指标"),
    "comparison": ("环比变化", "结构与增长", "最近完整月份与上一月对比"),
    "driver": ("量价驱动", "结构与增长", "数量指标与均值指标的变化"),
    "fulfillment": ("履约效率", "履约与支付", "延迟类比率及主要责任维度"),
    "payment": ("支付结构", "履约与支付", "支付方式或支付金额结构"),
    "reputation": ("口碑质量", "口碑与客户", "评分和低分相关指标"),
    "customer": ("客户沉淀", "口碑与客户", "复购或客户实体计数"),
    "seller": ("商家表现", "经营主体", "按商家拆分核心指标"),
    "product": ("商品表现", "经营主体", "按商品拆分核心指标"),
}


def _terms(metric: Metric) -> str:
    values = [metric.id, metric.name, *metric.synonyms]
    return " ".join(str(v).lower() for v in values if v)


def _dim_terms(sem: SemanticLayer, dim_id: str) -> str:
    dim = sem.dimension(dim_id)
    values = [
        dim.id,
        dim.raw.get("name_zh", ""),
        dim.raw.get("name", ""),
        *dim.raw.get("synonyms", []),
    ]
    return " ".join(str(v).lower() for v in values if v)


def _matches(text: str, pattern: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def _selected_metrics(sem: SemanticLayer) -> list[Metric]:
    return [
        metric for metric in sem.metrics.values()
        if metric.raw.get("selected", True)
    ]


def _supports(metric: Metric, dimensions: list[str]) -> bool:
    if not dimensions:
        return True
    supported = metric.supported_grain
    return all(dim in supported for dim in dimensions)


def _primary_metric(sem: SemanticLayer) -> Metric | None:
    candidates = []
    for metric in _selected_metrics(sem):
        if metric.type != "aggregate" or not metric.raw.get("additive"):
            continue
        terms = _terms(metric)
        score = int(metric.raw.get("analysis_priority") or 0)
        role = str(metric.raw.get("analysis_role") or "")
        if role in {"primary_sales", "sales_primary"}:
            score += 30
        if _matches(terms, r"商品销售额|销售额|销售收入|gmv|sales"):
            score += 14
        elif _matches(terms, r"金额|收入|营收|成交|revenue|amount"):
            score += 7
        elif "sum(" in str(metric.raw.get("expr", "")).lower():
            score += 5
        if _matches(terms, r"优惠券|信用卡|voucher|credit_card|支付"):
            score -= 12
        if _matches(terms, r"运费|freight"):
            score -= 4
        if _matches(terms, r"订单数|记录数|行数|客户数|商品数|count"):
            score -= 5
        if metric.raw.get("time_via"):
            score += 1
        candidates.append((score, metric.name, metric))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _find_metrics(sem: SemanticLayer, pattern: str,
                  types: set[str] | None = None) -> list[Metric]:
    found = []
    for metric in _selected_metrics(sem):
        if types and metric.type not in types:
            continue
        if _matches(_terms(metric), pattern):
            found.append(metric)
    return found


def _find_count_metric(sem: SemanticLayer) -> Metric | None:
    candidates = []
    for metric in _selected_metrics(sem):
        if metric.type != "aggregate":
            continue
        expr = str(metric.raw.get("expr", "")).upper()
        terms = _terms(metric)
        score = 0
        if "COUNT(DISTINCT" in expr:
            score += 4
        if "COUNT(" in expr:
            score += 2
        if _matches(terms, r"订单数|订单量|order|笔数|记录数|行数"):
            score += 4
        if score:
            candidates.append((score, metric))
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1] if candidates else None


def _find_dimension(sem: SemanticLayer, pattern: str) -> str | None:
    for dim_id in sem.dimensions:
        if _matches(_dim_terms(sem, dim_id), pattern):
            return dim_id
    return None


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _add_months(value: date, count: int) -> date:
    month = value.month - 1 + count
    return date(value.year + month // 12, month % 12 + 1, 1)


def _window(sem: SemanticLayer, focus: dict | None) -> dict:
    focus = focus or {}
    lower = _as_date(sem.time_range[0]) or date(1970, 1, 1)
    upper_exclusive = _as_date(sem.time_range[1]) or date.today()
    upper = max(lower + timedelta(days=1), upper_exclusive - timedelta(days=1))
    anchor = _as_date(focus.get("anchor_end"))
    if anchor is not None:
        anchor = min(max(anchor, lower), upper)

    # A report should not compare a partial latest month with a complete prior
    # month. Unless the caller supplied an earlier explicit anchor, use the
    # most recent complete calendar month.
    latest = upper_exclusive - timedelta(days=1)
    month_start = _month_start(latest)
    next_month = _add_months(month_start, 1)
    latest_complete = latest.day == (next_month - timedelta(days=1)).day
    if latest_complete:
        default_start, default_end = month_start, upper_exclusive
    else:
        default_end = month_start
        default_start = _add_months(default_end, -1)

    if anchor is not None and anchor < month_start:
        current_start = _month_start(anchor)
        current_end = min(upper_exclusive, _add_months(current_start, 1))
    else:
        current_start, current_end = default_start, default_end
    current_start = max(lower, current_start)
    if current_end <= current_start:
        current_end = min(upper_exclusive, current_start + timedelta(days=1))

    baseline_end = current_start
    baseline_start = max(lower, _add_months(current_start, -1))
    if baseline_start >= baseline_end:
        baseline_start = max(lower, baseline_end - timedelta(days=1))

    trend_start = max(lower, _add_months(current_start, -11))
    current = {"start": current_start.isoformat(), "end": current_end.isoformat()}
    baseline = {"start": baseline_start.isoformat(), "end": baseline_end.isoformat()}
    trend = {"start": trend_start.isoformat(), "end": current_end.isoformat()}
    comparison = {
        "label": f"{current_start:%Y-%m} 与上月对比",
        "mode": "period_vs_period",
        "current": current,
        "baseline": baseline,
    }
    filters = {
        dim: spec for dim, spec in (focus.get("filters") or {}).items()
        if isinstance(spec, dict) and spec.get("value") is not None
    }
    return {
        "current": current,
        "baseline": baseline,
        "trend": trend,
        "comparison": comparison,
        "filters": filters,
        "anchor_end": (anchor or current_start).isoformat(),
    }


def _append_item(items: list[AnalysisItem], module: str, metric: Metric,
                 dimensions: list[str], window: dict,
                 *, time: str = "current", compare: bool = False,
                 filters: bool = True, title: str | None = None) -> None:
    if not _supports(metric, dimensions):
        return
    slots: dict[str, Any] = {
        "metric": metric.id,
        "dimensions": dimensions,
        "limit": 10 if dimensions else 1,
    }
    if time:
        slots["time"] = dict(window[time])
    if compare:
        slots["comparison"] = dict(window["comparison"])
    if dimensions:
        slots["order_by"] = [{"field": "value", "direction": "desc"}]
    label, section, description = MODULE_META[module]
    items.append(AnalysisItem(
        id=f"an{len(items) + 1}",
        section=section,
        title=title or f"{description} · {metric.name}",
        slots=slots,
        module=module,
        apply_filters=filters,
    ))


def _all_items(sem: SemanticLayer, window: dict) -> list[AnalysisItem]:
    items: list[AnalysisItem] = []
    primary = _primary_metric(sem)
    month = _find_dimension(sem, r"月份|月度|^month$|month")

    if primary:
        _append_item(items, "overview", primary, [], window, time="")
        if month and primary.raw.get("time_via"):
            _append_item(
                items, "trend", primary, [month], window, time="trend",
                title=f"近 12 个月{primary.name}趋势",
            )
        if primary.raw.get("time_via"):
            _append_item(
                items, "comparison", primary, [], window,
                compare=True, title=f"{primary.name}月度变化",
            )

        structure_dims = []
        for pattern in (
            r"品类|类目|分类|类型|category|segment",
            r"渠道|channel",
            r"地区|省份|城市|region|province|city",
        ):
            dim = _find_dimension(sem, pattern)
            if dim and dim != month and dim not in structure_dims:
                structure_dims.append(dim)
        for dim in structure_dims[:2]:
            _append_item(
                items, "structure", primary, [dim], window, time="trend",
                filters=False, title=f"按{_dim_label(sem, dim)}看{primary.name}结构",
            )

    count_metric = _find_count_metric(sem)
    average_metric = next(iter(_find_metrics(
        sem, r"客单价|平均|均值|avg|average|单价", {"aggregate"}
    )), None)
    if count_metric and average_metric and average_metric.id != count_metric.id:
        _append_item(items, "driver", count_metric, [], window, compare=True)
        _append_item(items, "driver", average_metric, [], window, compare=True)

    delay_metrics = _find_metrics(sem, r"延迟|延误|delay|late", {"ratio"})
    seller_dim = _find_dimension(sem, r"商家|卖家|店铺|seller|store")
    if delay_metrics:
        _append_item(
            items, "fulfillment", delay_metrics[0],
            [seller_dim] if seller_dim else [], window,
        )

    payment_dim = _find_dimension(sem, r"支付方式|付款方式|payment")
    payment_metrics = _find_metrics(sem, r"支付|付款|payment")
    if payment_metrics:
        metric = payment_metrics[0]
        dims = [payment_dim] if payment_dim and _supports(metric, [payment_dim]) else []
        _append_item(items, "payment", metric, dims, window)

    reputation = _find_metrics(sem, r"评分|评价|口碑|review|score|低分")
    for metric in reputation[:2]:
        _append_item(items, "reputation", metric, [], window, compare=True)

    customer = next(iter(_find_metrics(
        sem, r"复购|客户|顾客|repeat|customer", {"entity_count"}
    )), None)
    if customer is None:
        customer = next(iter(_find_metrics(sem, r"复购|repeat", None)), None)
    if customer:
        _append_item(items, "customer", customer, [], window)

    if primary:
        for module, pattern in (
            ("seller", r"商家|卖家|店铺|seller|store"),
            ("product", r"商品|产品|product|sku"),
        ):
            dim = _find_dimension(sem, pattern)
            if dim:
                _append_item(
                    items, module, primary, [dim], window, time="trend",
                    filters=False,
                    title=f"按{_dim_label(sem, dim)}看{primary.name}",
                )

    # Keep stable ids after optional modules were skipped.
    for idx, item in enumerate(items, 1):
        item.id = f"an{idx}"
    return items


def _dim_label(sem: SemanticLayer, dim_id: str) -> str:
    dim = sem.dimension(dim_id)
    return str(dim.raw.get("name_zh", dim.id))


def _available_modules(items: list[AnalysisItem]) -> list[dict]:
    present = {item.module for item in items}
    modules = []
    for module, (label, section, description) in MODULE_META.items():
        if module not in present:
            continue
        modules.append({
            "id": module,
            "label": label,
            "section": section,
            "description": description,
            "default": module not in {"seller", "product"},
        })
    return modules


def module_catalog(sem: SemanticLayer,
                   focus: dict | None = None) -> list[dict]:
    window = _window(sem, focus)
    return _available_modules(_all_items(sem, window))


def build_analysis_items(sem: SemanticLayer, focus: dict | None = None,
                         modules: list[str] | None = None
                         ) -> tuple[list[AnalysisItem], dict]:
    window = _window(sem, focus)
    available = _available_modules(_all_items(sem, window))
    requested = None if modules is None else set(modules)
    if requested is None:
        selected = {m["id"] for m in available if m["default"]}
    else:
        selected = {m["id"] for m in available if m["id"] in requested}
    items = [
        item for item in _all_items(sem, window)
        if item.module in selected
    ]
    filters = window["filters"]
    if filters:
        for item in items:
            if item.apply_filters:
                item.slots = {**item.slots, "filters": dict(filters)}
    return items, window


def custom_analysis_item(slots: dict, title: str, item_id: str = "cx1"
                         ) -> AnalysisItem:
    data = dict(slots)
    data.setdefault("limit", 10 if data.get("dimensions") else 1)
    return AnalysisItem(
        id=item_id,
        section="自定义分析",
        title=title or "自定义分析",
        slots=data,
        module="custom",
        apply_filters=True,
    )


def analysis_hints(sem: SemanticLayer, dim_values_path: Path | str,
                   limit: int = 12) -> dict:
    path = Path(dim_values_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    values = data.get("values") or {}

    def candidates(pattern: str) -> list[str]:
        scored = []
        for dim_id in sem.dimensions:
            terms = _dim_terms(sem, dim_id)
            if not _matches(terms, pattern):
                continue
            needles = [
                str(dim_id), str(sem.dimension(dim_id).raw.get("column", "")),
                str(sem.dimension(dim_id).raw.get("filter_column", "")),
            ]
            for key, entries in values.items():
                if any(needle and needle in key for needle in needles):
                    scored.extend(str(value) for value in entries)
        return list(dict.fromkeys(scored))[:limit]

    return {
        "categories": candidates(r"品类|类目|分类|category"),
        "products": candidates(r"商品|产品|product|sku"),
        "sellers": candidates(r"商家|卖家|店铺|seller|store")[:8],
        "time_range": list(sem.time_range),
    }
