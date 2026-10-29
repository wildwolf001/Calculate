from __future__ import annotations

from app.datasets.merge import merge_semantics


CATALOG = {"tables": {"t": {"columns": [
    {"name": "c01"}, {"name": "c07"}, {"name": "c08"},
]}}}


def _old():
    return {
        "metrics": {
            "m_doc_delay": {
                "name_zh": "延迟订单",
                "provenance": "documentation",
                "fact": "t",
                "expr": "COUNT(DISTINCT CASE WHEN t.c07 > t.c08 THEN t.c01 END)",
                "supported_grain": ["none", "d_doc", "d_missing"],
                "data_health": {"rows": 1},
            }
        },
        "dimensions": {
            "d_doc": {"kind": "derived", "requires": "t", "name_zh": "是否延迟"},
        },
    }


def test_merge_restores_confirmed_metric_and_dimension():
    merged, warnings = merge_semantics(
        {"metrics": {"m_row": {"name_zh": "记录数"}}, "dimensions": {}},
        _old(), CATALOG,
    )
    assert "m_doc_delay" in merged["metrics"]
    assert "data_health" not in merged["metrics"]["m_doc_delay"]
    assert "d_doc" in merged["dimensions"]
    assert "d_doc" in merged["metrics"]["m_doc_delay"]["supported_grain"]
    assert "d_missing" not in merged["metrics"]["m_doc_delay"]["supported_grain"]
    assert warnings == []


def test_merge_skips_metric_whose_column_disappeared():
    old = _old()
    old["metrics"]["m_doc_delay"]["expr"] = "SUM(t.c99)"
    merged, warnings = merge_semantics({"metrics": {}, "dimensions": {}}, old, CATALOG)
    assert "m_doc_delay" not in merged["metrics"]
    assert warnings and "未保留" in warnings[0]


def test_merge_skips_auto_generated_metrics():
    old = {"metrics": {"m_auto": {"provenance": "auto", "expr": "SUM(t.c01)"}}}
    merged, _ = merge_semantics({"metrics": {}, "dimensions": {}}, old, CATALOG)
    assert "m_auto" not in merged["metrics"]
