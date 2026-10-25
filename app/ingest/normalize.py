"""Type / date / status normalization for raw e-commerce sheets.

The raw files are only read, never rewritten. Every coercion is recorded in
`ingest_log` so the quality report can explain what changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

TABLE_SPECS: dict[str, dict[str, Any]] = {
    "customers": {
        "pk": ["customer_id"],
        "timestamps": [],
        "ints": ["customer_zip_code"],
        "strs": ["customer_id", "customer_unique_id", "customer_city", "customer_state"],
    },
    "geolocation": {
        "pk": ["zip_code"],
        "timestamps": [],
        "ints": ["zip_code"],
        "floats": ["latitude", "longitude"],
        "strs": ["city", "state"],
    },
    "orders": {
        "pk": ["order_id"],
        "timestamps": ["purchase_at", "approved_at", "shipped_at", "delivered_at", "estimated_delivery_at"],
        "strs": ["order_id", "customer_id", "order_status"],
    },
    "order_items": {
        "pk": ["order_id", "order_item_id"],
        "timestamps": ["shipping_limit_at"],
        "ints": ["order_item_id"],
        "floats": ["price", "freight_value"],
        "strs": ["order_id", "product_id", "seller_id"],
    },
    "order_payments": {
        "pk": ["order_id", "payment_sequence"],
        "timestamps": [],
        "ints": ["payment_sequence", "payment_installments"],
        "floats": ["payment_value"],
        "strs": ["order_id", "payment_type"],
    },
    "order_reviews": {
        "pk": ["review_id"],
        "timestamps": ["review_created_at", "seller_answered_at"],
        "ints": ["review_score"],
        "strs": ["review_id", "order_id", "review_title", "review_message"],
    },
    "products": {
        "pk": ["product_id"],
        "timestamps": [],
        "ints": [
            "product_name_length", "product_description_length", "product_photos_count",
            "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm",
        ],
        "floats": ["base_price"],
        "strs": ["product_id", "category_id", "product_name"],
    },
    "sellers": {
        "pk": ["seller_id"],
        "timestamps": [],
        "ints": ["seller_zip_code"],
        "strs": ["seller_id", "seller_name", "seller_city", "seller_state"],
    },
    "categories": {
        "pk": ["category_id"],
        "timestamps": [],
        "strs": ["category_id", "category_name_en", "category_name_zh"],
    },
}


@dataclass
class NormalizeLog:
    table: str
    rows: int
    coerced: dict[str, int] = field(default_factory=dict)
    dropped: int = 0
    notes: list[str] = field(default_factory=list)


def _clean_str(series: pd.Series) -> pd.Series:
    out = series.astype("string").str.strip()
    return out.mask(out == "", pd.NA)


def normalize_table(name: str, df: pd.DataFrame) -> tuple[pd.DataFrame, NormalizeLog]:
    spec = TABLE_SPECS[name]
    log = NormalizeLog(table=name, rows=len(df))
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    for col in spec.get("strs", []):
        if col in df.columns:
            df[col] = _clean_str(df[col])

    for col in spec.get("ints", []):
        if col in df.columns:
            before = df[col]
            df[col] = pd.to_numeric(before, errors="coerce").round().astype("Int64")
            bad = int(before.notna().sum() - df[col].notna().sum())
            if bad:
                log.coerced[col] = bad

    for col in spec.get("floats", []):
        if col in df.columns:
            before = df[col]
            df[col] = pd.to_numeric(before, errors="coerce").astype("float64")
            bad = int(before.notna().sum() - df[col].notna().sum())
            if bad:
                log.coerced[col] = bad

    for col in spec.get("timestamps", []):
        if col in df.columns:
            before = df[col]
            parsed = pd.to_datetime(before, errors="coerce")
            bad = int(before.notna().sum() - parsed.notna().sum())
            if bad:
                log.coerced[col] = bad
            df[col] = parsed

    # Drop rows that duplicate the declared primary key; keep the first.
    pk = [c for c in spec.get("pk", []) if c in df.columns]
    if pk:
        dup = int(df.duplicated(subset=pk).sum())
        if dup:
            log.dropped = dup
            log.notes.append(f"dropped {dup} duplicate rows on PK {pk}")
            df = df.drop_duplicates(subset=pk, keep="first")

    # Drop fully-empty rows.
    empty = int(df.isna().all(axis=1).sum())
    if empty:
        log.dropped += empty
        df = df[~df.isna().all(axis=1)]

    return df.reset_index(drop=True), log
