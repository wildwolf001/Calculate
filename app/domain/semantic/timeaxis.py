"""Infer a human-readable event time axis from a column label."""
from __future__ import annotations

AXIS_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("review", "评价", "评论"), "评价时间"),
    (("delivered", "送达"), "送达时间"),
    (("shipped", "发货"), "发货时间"),
    (("approved", "审批", "审核"), "审批时间"),
    (("shipping_limit", "发货截止"), "发货截止时间"),
    (("purchase", "下单", "created_at", "order_date"), "下单时间"),
    (("payment", "支付"), "支付时间"),
]


def infer_time_axis(label: str | None) -> str | None:
    text = str(label or "").strip().lower()
    if not text:
        return None
    for hints, axis in AXIS_HINTS:
        if any(hint in text for hint in hints):
            return axis
    return None
