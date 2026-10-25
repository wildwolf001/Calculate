"""Independent golden-value check using hand-written SQL (no semantic layer).

Purpose: prove the warehouse itself is correct before the compiler exists.
Run: python scripts/sql_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from app.config.settings import WAREHOUSE_PATH  # noqa: E402

SQL = {
    "Q01": "SELECT COUNT(*) FROM orders WHERE order_status='delivered'",
    "Q02": "SELECT ROUND(SUM(i.price),2) FROM order_items i JOIN orders o USING(order_id) WHERE o.order_status='delivered'",
    "Q03": """
        WITH m AS (
          SELECT strftime(o.purchase_at,'%Y-%m') ym, SUM(i.price) amt
          FROM order_items i JOIN orders o USING(order_id)
          WHERE o.order_status='delivered' GROUP BY 1
        )
        SELECT ROUND(100.0 * ((SELECT amt FROM m WHERE ym='2025-04')
                            - (SELECT amt FROM m WHERE ym='2025-03'))
                    / (SELECT amt FROM m WHERE ym='2025-03'), 2)
    """,
    "Q04": """
        SELECT c.category_name_zh, ROUND(SUM(i.price),2)
        FROM order_items i
        JOIN orders o USING(order_id) JOIN products p USING(product_id)
        JOIN categories c USING(category_id)
        WHERE o.order_status='delivered' AND strftime(o.purchase_at,'%Y-%m')='2025-04'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1
    """,
    "Q05": """
        WITH per_seller_order AS (
          SELECT i.seller_id, o.order_id,
                 MAX(CASE WHEN o.delivered_at > o.estimated_delivery_at THEN 1 ELSE 0 END) late
          FROM order_items i JOIN orders o USING(order_id)
          WHERE o.order_status='delivered' AND strftime(o.purchase_at,'%Y-%m')='2025-05'
          GROUP BY 1,2
        ), agg AS (
          SELECT seller_id, COUNT(*) n, SUM(late) l FROM per_seller_order
          GROUP BY 1 HAVING COUNT(*) >= 10
        )
        SELECT s.seller_name, a.l, a.n, ROUND(100.0*a.l/a.n,2)
        FROM agg a JOIN sellers s USING(seller_id) ORDER BY 4 DESC LIMIT 1
    """,
    "Q06": """
        WITH d AS (
          SELECT CASE WHEN o.delivered_at > o.estimated_delivery_at THEN 'late' ELSE 'ontime' END k,
                 r.review_score
          FROM orders o JOIN order_reviews r USING(order_id) WHERE o.order_status='delivered'
        )
        SELECT k, COUNT(*), ROUND(AVG(review_score),2) FROM d GROUP BY 1 ORDER BY 1
    """,
    "Q07": """
        SELECT COUNT(*) FROM (
          SELECT c.customer_unique_id FROM orders o JOIN customers c USING(customer_id)
          WHERE o.order_status='delivered'
          GROUP BY 1 HAVING COUNT(DISTINCT o.order_id) >= 2
        )
    """,
}

EXPECTED = {
    "Q01": 10714,
    "Q02": 4858920.84,
    "Q03": 15.0,
    "Q04": "数码家电 / 105245.50",
    "Q05": "广州美妆个护商家004 / 91.53",
    "Q06": "late 2.00 / ontime 4.02",
    "Q07": 2378,
}


def main() -> int:
    con = duckdb.connect(str(WAREHOUSE_PATH), read_only=True)
    results = {}
    for qid, sql in SQL.items():
        results[qid] = con.execute(sql).fetchall()
    con.close()
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    ok = True
    if results["Q01"][0][0] != EXPECTED["Q01"]:
        ok = False
    if abs(results["Q02"][0][0] - EXPECTED["Q02"]) > 0.01:
        ok = False
    if abs(results["Q03"][0][0] - EXPECTED["Q03"]) > 0.01:
        ok = False
    if results["Q04"][0][0] != "数码家电" or abs(results["Q04"][0][1] - 105245.50) > 0.01:
        ok = False
    if results["Q05"][0][0] != "广州美妆个护商家004" or abs(results["Q05"][0][3] - 91.53) > 0.01:
        ok = False
    scores = {k: v for k, _, v in results["Q06"]}
    if abs(scores.get("late", 0) - 2.00) > 0.01 or abs(scores.get("ontime", 0) - 4.02) > 0.01:
        ok = False
    if results["Q07"][0][0] != EXPECTED["Q07"]:
        ok = False
    print("GOLDENS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
