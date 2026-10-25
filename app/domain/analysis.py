"""Deterministic plan set for the on-demand business analysis report.

Preset modules are fixed questions over the semantic layer; a user can select a
subset or add one custom block, which the controller parses into validated
plans. Session focus (filters + anchor period) is carried in so the report
stays relevant to what the user was just asking.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from app.config.settings import DIM_VALUES_PATH
from app.domain.semantic.loader import SemanticLayer


@dataclass
class AnalysisItem:
    id: str
    section: str
    title: str
    slots: dict
    module: str = ""
    # Some sections are intentionally global: a category filter would collapse
    # "品类结构" to a single row, and payment cannot be split by category.
    apply_filters: bool = True


ANALYSIS_MODULES = [
    {"id": "trend", "label": "规模与趋势", "section": "规模与趋势",
     "description": "近 12 个月商品销售额趋势", "default": True},
    {"id": "structure", "label": "品类结构", "section": "品类结构",
     "description": "品类销售额贡献与变化", "default": True},
    {"id": "driver", "label": "增长驱动", "section": "增长驱动",
     "description": "订单数 vs 客单价的变化", "default": True},
    {"id": "fulfillment", "label": "履约", "section": "履约与支付",
     "description": "商家延迟率（样本量 ≥10）", "default": True},
    {"id": "payment", "label": "支付", "section": "履约与支付",
     "description": "支付方式结构", "default": True},
    {"id": "reputation", "label": "口碑", "section": "口碑与客户",
     "description": "平均评分与低分率", "default": True},
    {"id": "customer", "label": "客户复购", "section": "口碑与客户",
     "description": "复购客户数", "default": True},
    {"id": "seller", "label": "商家表现", "section": "商家与商品",
     "description": "商家销售额贡献与变化", "default": False},
    {"id": "product", "label": "商品表现", "section": "商家与商品",
     "description": "商品销售额贡献与变化", "default": False},
]
MODULE_IDS = {m["id"] for m in ANALYSIS_MODULES}
DEFAULT_MODULES = {m["id"] for m in ANALYSIS_MODULES if m["default"]}


def module_catalog() -> list[dict]:
    return [dict(m) for m in ANALYSIS_MODULES]


def _as_date(value) -> date | None:
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


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _range(start: date, end: date) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat()}


def _all_items(current: dict, baseline: dict, comparison: dict,
               trend: dict) -> list[AnalysisItem]:
    return [
        AnalysisItem(
            "an1", "规模与趋势", "近 12 个月商品销售额趋势",
            {"metric": "gmv", "dimensions": ["month"], "time": trend, "limit": 100},
            module="trend",
        ),
        AnalysisItem(
            "an2", "品类结构", "品类销售额贡献与变化",
            {"metric": "gmv", "dimensions": ["category"], "comparison": comparison,
             "order_by": [{"field": "growth_rate", "direction": "desc"}], "limit": 10},
            module="structure", apply_filters=False,
        ),
        AnalysisItem(
            "an3", "增长驱动", "订单数变化",
            {"metric": "order_count", "comparison": comparison, "limit": 1},
            module="driver",
        ),
        AnalysisItem(
            "an4", "增长驱动", "客单价变化",
            {"metric": "avg_order_value", "comparison": comparison, "limit": 1},
            module="driver",
        ),
        AnalysisItem(
            "an5", "履约与支付", "商家延迟率",
            {"metric": "delay_rate", "dimensions": ["seller"], "time": current,
             "order_by": [{"field": "value", "direction": "desc"}],
             # Small sellers with 1/1 orders would otherwise top the ranking.
             "having": [{"metric": "order_count", "op": "gte", "value": 10}],
             "limit": 10},
            module="fulfillment",
        ),
        AnalysisItem(
            "an6", "履约与支付", "支付方式结构",
            {"metric": "paid_amount", "dimensions": ["payment_type"], "time": current,
             "order_by": [{"field": "value", "direction": "desc"}], "limit": 10},
            module="payment", apply_filters=False,
        ),
        AnalysisItem(
            "an7", "口碑与客户", "平均评分变化",
            {"metric": "avg_review_score", "comparison": comparison, "limit": 1},
            module="reputation",
        ),
        AnalysisItem(
            "an8", "口碑与客户", "低分率变化",
            {"metric": "low_score_rate", "comparison": comparison, "limit": 1},
            module="reputation",
        ),
        AnalysisItem(
            "an9", "口碑与客户", "复购客户数",
            {"metric": "repeat_customer_count", "time": current, "limit": 1},
            module="customer",
        ),
        AnalysisItem(
            "an10", "商家与商品", "商家销售额贡献与变化",
            {"metric": "gmv", "dimensions": ["seller"], "comparison": comparison,
             "order_by": [{"field": "growth_rate", "direction": "desc"}], "limit": 10},
            module="seller", apply_filters=False,
        ),
        AnalysisItem(
            "an11", "商家与商品", "商品销售额贡献与变化",
            {"metric": "gmv", "dimensions": ["product"], "comparison": comparison,
             "order_by": [{"field": "growth_rate", "direction": "desc"}], "limit": 10},
            module="product", apply_filters=False,
        ),
    ]


def build_analysis_items(sem: SemanticLayer, focus: dict | None = None,
                         modules: list[str] | None = None
                         ) -> tuple[list[AnalysisItem], dict]:
    """Return (items, window info).

    `modules=None` means the default set; `modules=[]` means no preset items
    (used when the user only supplied a custom analysis block).
    """
    focus = focus or {}
    filters = {
        dim: spec for dim, spec in (focus.get("filters") or {}).items()
        if isinstance(spec, dict) and spec.get("value") is not None
    }
    lo = _as_date(sem.time_range[0]) or date(1970, 1, 1)
    hi = _as_date(sem.time_range[1]) or date.today()
    anchor = _as_date(focus.get("anchor_end")) or hi
    anchor = min(max(anchor, lo), hi)

    current_end = max(_month_start(anchor), _add_months(_month_start(lo), 1))
    current_start = max(lo, _add_months(current_end, -1))
    baseline_start = max(lo, _add_months(current_start, -1))
    trend_start = max(lo, _add_months(current_end, -12))

    current = _range(current_start, current_end)
    baseline = _range(baseline_start, current_start)
    trend = _range(trend_start, current_end)
    comparison = {
        "label": (
            f"{current_start.strftime('%Y-%m')} vs {baseline_start.strftime('%Y-%m')}"
        ),
        "mode": "period_vs_period",
        "current": current,
        "baseline": baseline,
    }

    selected = DEFAULT_MODULES if modules is None else (
        {m for m in modules if m in MODULE_IDS}
    )
    items = [
        item for item in _all_items(current, baseline, comparison, trend)
        if item.module in selected
    ]

    if filters:
        for item in items:
            if item.apply_filters:
                item.slots = {**item.slots, "filters": dict(filters)}

    window = {
        "current": current,
        "baseline": baseline,
        "trend": trend,
        "anchor_end": anchor.isoformat(),
        "filters": filters,
    }
    return items, window


def analysis_hints(sem: SemanticLayer, limit: int = 12) -> dict:
    """Small value catalog shown beside the custom-analysis input."""
    try:
        data = json.loads(DIM_VALUES_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    values = data.get("values") or {}
    categories = [str(v) for v in (values.get("categories.category_name_zh") or [])]
    products = (
        data.get("product_stem_list")
        or list((data.get("product_stems") or {}).keys())
    )
    sellers = [str(v) for v in (values.get("sellers.seller_name") or [])]
    return {
        "categories": categories,
        "products": [str(v) for v in products[:limit]],
        "sellers": sellers[:8],
        "time_range": list(sem.time_range),
    }
