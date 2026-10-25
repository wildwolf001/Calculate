"""Templates for dimension-member listings.

The SQL itself lives in the compiler (`member_list` plan shape); this module
only holds the display labels and the deterministic answer template.
"""
from __future__ import annotations

DIM_LABELS = {
    "category": "品类",
    "seller": "商家",
    "product": "商品",
    "state": "省份",
    "city": "城市",
    "payment_type": "支付方式",
}


def describe(dim: str, rows: list[list], total: int) -> str:
    label = DIM_LABELS.get(dim, dim)
    if not rows:
        return f"当前范围内没有可用的{label}。"
    names = [str(r[0]) for r in rows[:10]]
    more = "" if len(rows) <= 10 else f" 等 {len(rows)} 个"
    lines = [
        f"共有 **{total} 个{label}**：",
        "、".join(names) + more + "。",
        "",
        "每个{0}覆盖的已送达订单数见右侧表格。".format(label),
    ]
    if len(rows) > 10:
        lines.append("完整清单请在右侧表格查看或导出 CSV。")
    return "\n".join(lines)
