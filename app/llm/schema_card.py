"""Compact schema/value card used by the parser prompt.

Kept intentionally small: the model needs ids, names, synonyms and a few
example values; it does not need raw rows.
"""
from __future__ import annotations

import json

from app.config.settings import DIM_VALUES_PATH
from app.domain.semantic.loader import SemanticLayer

VALUE_KEYS = {
    "category": "categories.category_name_zh",
    "seller": "sellers.seller_name",
    "product": "products.product_name",
    "state": "customers.customer_state",
    "city": "customers.customer_city",
    "payment_type": "order_payments.payment_type",
    "order_status": "orders.order_status",
}


def build_schema_card(sem: SemanticLayer, max_values: int = 12,
                      dim_values_path=DIM_VALUES_PATH) -> str:
    lines: list[str] = []
    lines.append(f"数据时间范围: {sem.time_range[0]} 至 {sem.time_range[1]}（右开）")
    lines.append("\n指标（metric_id: 中文名; 同义词; 支持维度; 口径）:")
    for m in sem.metrics.values():
        grains = [g for g in m.raw.get("supported_grain", []) if g != "none"]
        lines.append(
            f"- {m.id}: {m.name}; 同义词={m.synonyms}; 支持维度={grains}; "
            f"口径={m.raw.get('definition', '')}"
        )
    lines.append("\n维度（dimension_id: 中文名; 同义词）:")
    for d in sem.dimensions.values():
        lines.append(f"- {d.id}: {d.raw.get('name_zh', d.id)}; {d.raw.get('synonyms', [])}")

    try:
        data = json.loads(dim_values_path.read_text(encoding="utf-8"))
        values = data.get("values", {})
        lines.append("\n维度取值样例（必须使用真实值，不要编造）:")
        for dim, key in VALUE_KEYS.items():
            vals = values.get(key, [])
            if vals:
                sample = ", ".join(map(str, vals[:max_values]))
                more = f" …共{len(vals)}个" if len(vals) > max_values else ""
                lines.append(f"- {dim}: {sample}{more}")
        stems = data.get("product_stem_list") or list((data.get("product_stems") or {}).keys())
        if stems:
            sample = ", ".join(stems[:20])
            more = f" …共{len(stems)}族" if len(stems) > 20 else ""
            lines.append(
                f"- product 商品族（可用关键词/前缀筛选）: {sample}{more}"
            )
    except Exception:
        pass

    lines.append("\n规则:")
    lines.append("- 商品销售额不含运费；支付金额必须先按订单汇总。")
    lines.append("- 未写年份时按数据中最近一次出现理解（2025）。")
    lines.append("- 不支持/未定义的指标放在 unsupported，不要编造。")
    return "\n".join(lines)
