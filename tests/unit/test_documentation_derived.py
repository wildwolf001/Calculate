from __future__ import annotations

from app.ingest.documentation import (
    CatalogIndex,
    _apply_derived_dimensions,
    _conditions_from_metrics,
    _dimension_subject,
)


def _index() -> CatalogIndex:
    index = CatalogIndex(dataset=None)
    index.columns = {"t_005": {"c01": "c01", "c04": "c04", "c07": "c07", "c08": "c08"}}
    index.columns_by_label = {
        "delivered_at": [("t_005", "c07")],
        "estimated_delivery_at": [("t_005", "c08")],
        "order_id": [("t_005", "c01")],
        "purchase_at": [("t_005", "c04")],
    }
    index.types = {"t_005": {"c07": "datetime", "c08": "datetime"}}
    index.rows = {"t_005": 100}
    return index


def _semantic() -> dict:
    return {
        "tables": {
            "t_003": {"grain": ["c01"], "time": None},
            "t_005": {"grain": ["c01"], "time": "c04"},
            "t_008": {"grain": ["c01"], "time": "c06"},
        },
        "joins": {"t_008": {"t_005": {"left": "c02", "right": "c01", "card": "N:1"}}},
        "dimensions": {},
        "metrics": {
            "m_score": {
                "name_zh": "评分", "type": "aggregate", "fact": "t_008",
                "supported_grain": ["none"],
            },
            "m_cust": {
                "name_zh": "客户数", "type": "aggregate", "fact": "t_003",
                "supported_grain": ["none"],
            },
            "m_ontime": {
                "name_zh": "准时交付率", "type": "ratio", "fact": "t_005",
                "numerator": "COUNT(CASE WHEN t_005.c07 <= t_005.c08 THEN 1 END)",
                "supported_grain": ["none"],
            },
        },
    }


def test_subject_strips_business_suffix():
    assert _dimension_subject("延迟订单") == "延迟"
    assert _dimension_subject("准时交付率") == "准时"
    assert _dimension_subject("低分评价") == "低分"


def test_conditions_are_read_from_existing_metrics():
    found = _conditions_from_metrics(_semantic())
    assert found[("t_005", "c07", "c08")]["le"] == "准时交付率"


def test_complementary_formulas_create_a_derived_dimension():
    semantic = _semantic()
    rules = [
        {
            "name": "延迟订单",
            "formula": "delivered_at > estimated_delivery_at",
            "aggregation": "count_distinct",
            "table_id": "t_005",
        },
    ]
    warnings: list[str] = []
    created = _apply_derived_dimensions(semantic, rules, _index(), warnings)

    assert created == ["d_doc_t_005_c07_c08"]
    dim = semantic["dimensions"]["d_doc_t_005_c07_c08"]
    assert dim["kind"] == "derived"
    assert dim["labels"] == {"true": "延迟", "false": "准时"}
    assert dim["requires"] == "t_005"
    assert dim["expr"] == "t_005.c07 > t_005.c08"
    assert warnings and "派生维度" in warnings[0]


def test_derived_dimension_expands_only_reachable_metrics():
    semantic = _semantic()
    rules = [
        {
            "name": "延迟订单",
            "formula": "delivered_at > estimated_delivery_at",
            "aggregation": "count_distinct",
            "table_id": "t_005",
        },
    ]
    created = _apply_derived_dimensions(semantic, rules, _index(), [])
    dim_id = created[0]
    assert dim_id in semantic["metrics"]["m_score"]["supported_grain"]
    assert dim_id in semantic["metrics"]["m_ontime"]["supported_grain"]
    assert dim_id not in semantic["metrics"]["m_cust"]["supported_grain"]


def test_single_sided_formula_uses_business_complement():
    semantic = _semantic()
    semantic["metrics"].pop("m_ontime")
    rules = [
        {
            "name": "延迟订单",
            "formula": "delivered_at > estimated_delivery_at",
            "aggregation": "count_distinct",
            "table_id": "t_005",
        },
    ]
    created = _apply_derived_dimensions(semantic, rules, _index(), [])
    assert created == ["d_doc_t_005_c07_c08"]
    dim = semantic["dimensions"][created[0]]
    assert dim["labels"] == {"true": "延迟", "false": "准时"}
    assert "准时订单" in dim["synonyms"]


def test_derive_dimensions_from_metrics_updates_draft():
    from app.ingest.documentation import derive_dimensions_from_metrics

    semantic = _semantic()
    semantic["metrics"]["m_delay"] = {
        "name_zh": "延迟率",
        "type": "ratio",
        "fact": "t_005",
        "numerator": "COUNT(CASE WHEN t_005.c07 > t_005.c08 THEN 1 END)",
    }
    draft = {"dimensions": [], "selected_dimensions": []}
    warnings: list[str] = []

    created = derive_dimensions_from_metrics(semantic, draft, warnings)

    assert created == ["d_doc_t_005_c07_c08"]
    assert draft["selected_dimensions"] == created
    assert draft["dimensions"][0]["kind"] == "derived"
    assert any("派生维度" in warning for warning in warnings)


def test_delay_subject_normalizes_to_late_and_ontime():
    semantic = {
        "metrics": {
            "m_delay": {
                "name_zh": "延迟送达率",
                "type": "ratio",
                "fact": "t_005",
                "numerator": "COUNT(CASE WHEN t_005.c07 > t_005.c08 THEN 1 END)",
            }
        },
        "dimensions": {},
        "joins": {},
    }
    created = _apply_derived_dimensions(semantic, [], None, [])
    assert created == ["d_doc_t_005_c07_c08"]
    assert semantic["dimensions"][created[0]]["labels"] == {
        "true": "延迟", "false": "准时",
    }
