"""Value-level catalogs used by grounding + alias resolution.

`dim_values` maps dimension columns to their distinct values. `dim_aliases`
adds hand-maintained synonyms (e.g. 电子产品 -> 数码家电) on top of exact
values; aliases never override an exact match.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

from app.config.settings import WAREHOUSE_PATH

DIMENSION_COLUMNS = {
    "categories": ["category_id", "category_name_en", "category_name_zh"],
    "sellers": ["seller_id", "seller_name", "seller_city", "seller_state"],
    "customers": ["customer_state", "customer_city"],
    "products": ["product_name"],
    "orders": ["order_status"],
    "order_payments": ["payment_type"],
    "geolocation": ["state", "city"],
}

# Manually curated synonyms. Keys are canonical values in the data; values are
# alternative phrasings a business user might type.
MANUAL_ALIASES: dict[str, dict[str, list[str]]] = {
    "categories.category_name_zh": {
        "数码家电": ["数码", "家电", "电子产品", "电子", "3C", "数码产品"],
        "家居生活": ["家居", "生活用品", "家居家装"],
        "食品饮料": ["食品", "饮料", "吃的", "零食"],
        "美妆个护": ["美妆", "个护", "化妆", "护肤", "化妆品"],
        "运动户外": ["运动", "户外", "体育"],
        "图书文教": ["图书", "书籍", "文教", "文具", "书"],
    },
    "order_payments.payment_type": {
        "credit_card": ["信用卡"],
        "debit_card": ["借记卡", "储蓄卡"],
        "voucher": ["代金券", "优惠券", "券"],
        "bank_transfer": ["银行转账", "转账"],
    },
    "orders.order_status": {
        "delivered": ["已送达", "已完成", "已收货"],
        "cancelled": ["已取消", "取消"],
        "unavailable": ["不可用", "无货"],
        "shipped": ["已发货", "配送中"],
    },
}


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_dim_values(db_path: Path = WAREHOUSE_PATH) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        out: dict = {"values": {}, "aliases": MANUAL_ALIASES}
        for table, cols in DIMENSION_COLUMNS.items():
            for col in cols:
                key = f"{table}.{col}"
                rows = con.execute(
                    f"SELECT DISTINCT {_quote(col)} FROM {_quote(table)} "
                    f"WHERE {_quote(col)} IS NOT NULL ORDER BY 1"
                ).fetchall()
                vals = [r[0].isoformat() if hasattr(r[0], "isoformat") else r[0] for r in rows]
                if len(vals) <= 5000:
                    out["values"][key] = vals
        # Human readable aliases: seller names map to ids.
        seller_rows = con.execute("SELECT seller_id, seller_name FROM sellers ORDER BY 1").fetchall()
        out["seller_names"] = {name: sid for sid, name in seller_rows if name}
        out["seller_ids"] = {sid: name for sid, name in seller_rows if name}
        # Generic lookup -> id maps for grounding.
        product_names = [r[0] for r in con.execute(
            "SELECT product_name FROM products ORDER BY product_id"
        ).fetchall() if r[0]]
        stems: dict[str, list[str]] = {}
        for name in product_names:
            stem = re.sub(r"\d+款$", "", name).strip() or name
            stems.setdefault(stem, []).append(name)
        out["product_stems"] = stems
        out["product_stem_list"] = sorted(stems)

        out["id_maps"] = {
            "category": {
                name: cid for cid, name in con.execute(
                    "SELECT category_id, category_name_zh FROM categories"
                ).fetchall()
            },
            "seller": out["seller_names"],
            "product": {
                name: pid for pid, name in con.execute(
                    "SELECT product_id, product_name FROM products"
                ).fetchall()
            },
        }
        return out
    finally:
        con.close()
