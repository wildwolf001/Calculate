"""Structured LLM specs are built server-side and must pass execution probes."""
from __future__ import annotations

import duckdb
import pytest
import yaml

from app.ingest.generic_pipeline import analyze_files
from app.ingest.llm_semantic import _build_metric, _table_map


def _store(tmp_path):
    files = {
        "orders.csv": (
            "order_id,customer_id,order_status,purchase_at,delivered_at,estimated_delivery_at\n"
            "O1,C1,delivered,2025-01-01 10:00:00,2025-01-05 10:00:00,2025-01-04 10:00:00\n"
            "O2,C2,delivered,2025-01-02 10:00:00,2025-01-06 10:00:00,2025-01-08 10:00:00\n"
            "O3,C1,cancelled,2025-01-03 10:00:00,,2025-01-09 10:00:00\n"
            "O4,C1,delivered,2025-01-04 10:00:00,2025-01-07 10:00:00,2025-01-09 10:00:00\n"
        ),
        "order_items.csv": (
            "order_id,order_item_id,product_id,price\n"
            "O1,1,P1,100\nO1,2,P2,50\nO2,1,P1,200\nO3,1,P1,70\nO4,1,P2,25\n"
        ),
        "customers.csv": (
            "customer_id,customer_unique_id,customer_city\n"
            "C1,U1,北京\nC2,U2,上海\n"
        ),
    }
    paths = []
    for name, text in files.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def test_structured_specs_compile_and_probe(tmp_path):
    root = tmp_path / "ds"
    out = analyze_files(_store(tmp_path), root, "ds_llm", "LLM 草稿")

    semantic = yaml.safe_load((root / "semantic.yaml").read_text(encoding="utf-8"))
    binding = yaml.safe_load((root / "binding.yaml").read_text(encoding="utf-8"))
    labels, columns = _table_map(out.tables)
    con = duckdb.connect(str(root / "warehouse.duckdb"), read_only=True)
    probes = {"count": 0}
    try:
        gmv_id, gmv, _ = _build_metric(
            {
                "name": "商品销售额",
                "fact": "order_items",
                "aggregation": "sum",
                "column": "price",
                "filters": [{"table": "orders", "column": "order_status",
                             "value": "delivered"}],
                "unit": "元",
            },
            semantic, binding, out.tables, labels, columns, con, probes,
        )
        assert gmv["expr"].startswith("SUM(")
        assert gmv["fact"] == labels["order_items"]
        assert len(gmv["scope"]) == 1
        assert next(iter(gmv["scope"])).startswith(labels["orders"] + ".")

        rate_id, rate, _ = _build_metric(
            {
                "name": "延迟率",
                "fact": "orders",
                "aggregation": "ratio",
                "numerator_condition": {
                    "table": "orders", "left": "delivered_at", "op": ">",
                    "right": "estimated_delivery_at",
                },
                "denominator": {"kind": "count_distinct", "table": "orders",
                                "column": "order_id"},
                "filters": [{"table": "orders", "column": "order_status",
                             "value": "delivered"}],
                "unit": "%",
            },
            semantic, binding, out.tables, labels, columns, con, probes,
        )
        assert rate["type"] == "ratio"
        assert rate["grain_numerator"].startswith("MAX(CASE WHEN")

        repeat_id, repeat, _ = _build_metric(
            {
                "name": "复购客户",
                "fact": "orders",
                "aggregation": "entity_count",
                "entity": {"table": "customers", "column": "customer_unique_id"},
                "min_orders": 2,
                "filters": [{"table": "orders", "column": "order_status",
                             "value": "delivered"}],
                "unit": "人",
            },
            semantic, binding, out.tables, labels, columns, con, probes,
        )
        assert repeat["type"] == "entity_count"
        assert repeat["entity"].startswith(labels["customers"] + ".")
        assert repeat["min_orders"] == 2
        assert probes["count"] >= 3
    finally:
        con.close()


def test_invalid_filter_value_is_rejected(tmp_path):
    root = tmp_path / "ds"
    out = analyze_files(_store(tmp_path), root, "ds_llm_bad", "LLM 草稿")
    semantic = yaml.safe_load((root / "semantic.yaml").read_text(encoding="utf-8"))
    binding = yaml.safe_load((root / "binding.yaml").read_text(encoding="utf-8"))
    labels, columns = _table_map(out.tables)
    con = duckdb.connect(str(root / "warehouse.duckdb"), read_only=True)
    try:
        with pytest.raises(ValueError):
            _build_metric(
                {
                    "name": "错误口径",
                    "fact": "orders",
                    "aggregation": "count",
                    "column": "order_id",
                    "filters": [{"table": "orders", "column": "order_status",
                                 "value": "不存在"}],
                },
                semantic, binding, out.tables, labels, columns, con,
                {"count": 0},
            )
    finally:
        con.close()
