from __future__ import annotations

from app.domain.semantic.loader import materialize_semantic


def _semantic() -> dict:
    return {
        "version": 1,
        "dataset": "fixture",
        "tables": {
            "customers": {"grain": ["customer_id"], "time": None,
                          "columns": ["customer_id", "customer_unique_id"]},
            "orders": {"grain": ["order_id"], "time": "order_date",
                       "columns": ["order_id", "customer_id", "status", "order_date"]},
        },
        "joins": {
            "orders": {"customers": {"left": "customer_id",
                                      "right": "customer_id", "card": "N:1"}},
        },
        "metrics": {
            "repeat_customer_count": {
                "name_zh": "复购客户数", "type": "entity_count",
                "fact": "orders", "entity": "customers.customer_unique_id",
                "entity_join": "orders -> customers",
                "count_orders": "COUNT(DISTINCT orders.order_id)",
                "min_orders": 2, "scope": {"orders.status": "delivered"},
                "time_via": "orders.order_date",
                "supported_grain": ["none"], "unit": "人",
            },
        },
        "dimensions": {
            "status": {"name_zh": "状态", "kind": "categorical",
                       "column": "orders.status"},
            "month": {"name_zh": "月份", "kind": "time",
                      "column": "orders.order_date", "format": "%Y-%m"},
        },
        "time_range": ["2025-01-01", "2025-12-31"],
        "capabilities": {"entity_count": True},
    }


def test_materialize_writes_entity_registry_and_generated_metric():
    semantic = _semantic()
    out = materialize_semantic(
        semantic, semantic["metrics"], semantic["dimensions"],
        keep_dims=["status", "month"],
    )
    assert "customer" not in out["entities"]  # derived id is the table id
    assert "customers" in out["entities"]
    generated = out["metrics"]["entity_count_customers"]
    assert generated["type"] == "entity_count"
    assert generated["min_orders"] == 1
    assert set(generated["supported_grain"]) == {"none", "month", "status"}
    names = [metric["name_zh"] for metric in out["metrics"].values()]
    assert len(names) == len(set(names))
