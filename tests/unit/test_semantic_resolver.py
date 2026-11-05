from __future__ import annotations

from app.domain.semantic.loader import Metric, SemanticLayer
from app.domain.semantic.resolver import SemanticResolver


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="resolver",
        tables={"orders": {"grain": ["c01"], "time": "c04", "columns": ["c01", "c04"]}},
        joins={},
        pre_aggregates={},
        metrics={
            "m_delay": Metric("m_delay", {
                "name_zh": "配送延迟率", "type": "ratio", "fact": "orders",
                "numerator": "late", "denominator": "all",
                "synonyms": ["配送延迟率", "延迟送达率"],
            }),
            "m_doc_delay": Metric("m_doc_delay", {
                "name_zh": "延迟订单", "type": "aggregate", "fact": "orders",
                "expr": "COUNT(*)",
                "synonyms": ["延迟订单", "延迟订单数"],
            }),
            "m_sales": Metric("m_sales", {
                "name_zh": "销售额", "type": "aggregate", "fact": "orders",
                "expr": "SUM(orders.c04)",
            }),
            "m_paid_sales": Metric("m_paid_sales", {
                "name_zh": "已送达商品销售额", "type": "aggregate", "fact": "orders",
                "expr": "SUM(orders.c04)",
            }),
        },
        dimensions={},
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        raw={},
    )


def test_resolves_partial_name_to_ratio_metric():
    result = SemanticResolver(_sem()).resolve(
        "5月已送达订单不少于3笔的商家中，延迟率最高的是谁？"
    )
    assert result.status == "unique"
    assert result.selected == "m_delay"


def test_exact_name_wins_over_longer_name():
    result = SemanticResolver(_sem()).resolve("2025年上半年销售额是多少？")
    assert result.status == "unique"
    assert result.selected == "m_sales"


def test_missing_metric_is_reported():
    result = SemanticResolver(_sem()).resolve("今天天气怎么样？")
    assert result.status == "missing"


def test_ambiguous_is_not_guessed():
    result = SemanticResolver(_sem()).resolve("延迟")
    assert result.status in {"ambiguous", "unique"}
    if result.status == "ambiguous":
        assert result.selected is None
