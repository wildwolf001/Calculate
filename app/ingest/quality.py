"""Data quality checks. Findings are descriptive, never auto-fixed.

Each check has {id, severity, title, count, detail}. `severity` is one of
info / warn / error. Errors block ingest-driven claims in the UI, warnings are
surfaced next to results.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.config.settings import WAREHOUSE_PATH

CHECKS = [
    (
        "pk_duplicates",
        "error",
        "主键重复",
        """
        SELECT 'orders' t, COUNT(*) - COUNT(DISTINCT order_id) n FROM orders
        UNION ALL SELECT 'order_items', COUNT(*) - COUNT(DISTINCT (order_id || '|' || order_item_id)) FROM order_items
        UNION ALL SELECT 'order_payments', COUNT(*) - COUNT(DISTINCT (order_id || '|' || payment_sequence)) FROM order_payments
        UNION ALL SELECT 'order_reviews', COUNT(*) - COUNT(DISTINCT review_id) FROM order_reviews
        UNION ALL SELECT 'customers', COUNT(*) - COUNT(DISTINCT customer_id) FROM customers
        UNION ALL SELECT 'products', COUNT(*) - COUNT(DISTINCT product_id) FROM products
        UNION ALL SELECT 'sellers', COUNT(*) - COUNT(DISTINCT seller_id) FROM sellers
        """,
    ),
    (
        "orphan_orders",
        "error",
        "订单明细/支付/评价引用了不存在的订单",
        """
        SELECT 'order_items' t, COUNT(*) n FROM order_items i
          LEFT JOIN orders o USING (order_id) WHERE o.order_id IS NULL
        UNION ALL SELECT 'order_payments', COUNT(*) FROM order_payments p
          LEFT JOIN orders o USING (order_id) WHERE o.order_id IS NULL
        UNION ALL SELECT 'order_reviews', COUNT(*) FROM order_reviews r
          LEFT JOIN orders o USING (order_id) WHERE o.order_id IS NULL
        """,
    ),
    (
        "orphan_dims",
        "error",
        "明细引用了不存在的商品/商家",
        """
        SELECT 'order_items.product_id' t, COUNT(*) n FROM order_items i
          LEFT JOIN products p USING (product_id) WHERE p.product_id IS NULL
        UNION ALL SELECT 'order_items.seller_id', COUNT(*) FROM order_items i
          LEFT JOIN sellers s USING (seller_id) WHERE s.seller_id IS NULL
        UNION ALL SELECT 'products.category_id', COUNT(*) FROM products p
          LEFT JOIN categories c USING (category_id) WHERE c.category_id IS NULL
        """,
    ),
    (
        "delivered_missing_payment",
        "warn",
        "已送达订单没有支付记录",
        """
        SELECT 'delivered' t, COUNT(*) n FROM orders o
        WHERE o.order_status='delivered'
          AND NOT EXISTS (SELECT 1 FROM order_payments p WHERE p.order_id=o.order_id)
        """,
    ),
    (
        "delivered_missing_review",
        "info",
        "已送达订单没有评价（评分类指标覆盖不全）",
        """
        SELECT 'delivered' t, COUNT(*) n FROM orders o
        WHERE o.order_status='delivered'
          AND NOT EXISTS (SELECT 1 FROM order_reviews r WHERE r.order_id=o.order_id)
        """,
    ),
    (
        "delivered_missing_delivered_at",
        "error",
        "已送达订单缺少 delivered_at",
        "SELECT 'orders' t, COUNT(*) n FROM orders WHERE order_status='delivered' AND delivered_at IS NULL",
    ),
    (
        "date_order_violations",
        "warn",
        "日期顺序异常（下单/审批/发货/送达）",
        """
        SELECT 'approved_before_purchase' t, COUNT(*) n FROM orders
          WHERE approved_at < purchase_at
        UNION ALL SELECT 'shipped_before_purchase', COUNT(*) FROM orders WHERE shipped_at < purchase_at
        UNION ALL SELECT 'delivered_before_shipped', COUNT(*) FROM orders WHERE delivered_at < shipped_at
        UNION ALL SELECT 'estimated_before_purchase', COUNT(*) FROM orders WHERE estimated_delivery_at < purchase_at
        """,
    ),
    (
        "payment_reconciliation",
        "warn",
        "支付金额与明细金额+运费不一致",
        """
        WITH items AS (
          SELECT order_id, SUM(price) price, SUM(freight_value) freight FROM order_items GROUP BY 1
        ), pays AS (
          SELECT order_id, SUM(payment_value) paid FROM order_payments GROUP BY 1
        )
        SELECT 'mismatch_gt_0.01' t, COUNT(*) n FROM items i JOIN pays p USING (order_id)
        WHERE ABS(i.price + i.freight - p.paid) > 0.01
        """,
    ),
    (
        "invalid_values",
        "error",
        "非法值（负金额/评分越界/无效状态）",
        """
        SELECT 'negative_price' t, COUNT(*) n FROM order_items WHERE price < 0
        UNION ALL SELECT 'negative_freight', COUNT(*) FROM order_items WHERE freight_value < 0
        UNION ALL SELECT 'negative_payment', COUNT(*) FROM order_payments WHERE payment_value < 0
        UNION ALL SELECT 'review_out_of_range', COUNT(*) FROM order_reviews WHERE review_score NOT BETWEEN 1 AND 5
        UNION ALL SELECT 'unknown_status', COUNT(*) FROM orders
          WHERE order_status NOT IN ('delivered','cancelled','unavailable','shipped')
        """,
    ),
    (
        "multi_seller_orders",
        "info",
        "一单跨多商家（按商家汇总时需去重订单）",
        """
        SELECT 'orders' t, COUNT(*) n FROM (
          SELECT order_id FROM order_items GROUP BY 1 HAVING COUNT(DISTINCT seller_id) > 1
        )
        """,
    ),
    (
        "multi_category_orders",
        "info",
        "一单跨多品类（按品类汇总时需去重订单）",
        """
        SELECT 'orders' t, COUNT(*) n FROM (
          SELECT i.order_id FROM order_items i JOIN products p USING (product_id)
          GROUP BY 1 HAVING COUNT(DISTINCT p.category_id) > 1
        )
        """,
    ),
    (
        "multi_payment_orders",
        "info",
        "一单多笔支付（必须先按订单汇总）",
        """
        SELECT 'orders' t, COUNT(*) n FROM (
          SELECT order_id FROM order_payments GROUP BY 1 HAVING COUNT(*) > 1
        )
        """,
    ),
    (
        "status_distribution",
        "info",
        "订单状态分布",
        "SELECT order_status t, COUNT(*) n FROM orders GROUP BY 1 ORDER BY 2 DESC",
    ),
]


def run_quality_checks(db_path: Path = WAREHOUSE_PATH) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    results = []
    try:
        for check_id, severity, title, sql in CHECKS:
            try:
                rows = con.execute(sql).fetchall()
            except Exception as exc:  # a check must not abort the report
                rows = [("__error__", str(exc))]
            detail = {str(t): n for t, n in rows}
            count = sum(v for v in detail.values() if isinstance(v, int) and v > 0)
            if check_id == "status_distribution":
                count = 0
            results.append({
                "id": check_id,
                "severity": severity,
                "title": title,
                "count": count,
                "detail": detail,
            })
    finally:
        con.close()
    return {"checks": results, "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
