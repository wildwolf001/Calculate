from __future__ import annotations

import pytest

from app.domain.plan import FilterSpec, OrderSpec, QueryPlan
from app.domain.pipeline import PipelineError, prepare
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer, SemanticError


def _sem(metric: Metric, dimensions=None) -> SemanticLayer:
    return SemanticLayer(
        version=1, dataset="t",
        tables={
            "t": {"grain": ["c01"], "time": None, "columns": ["c01", "c02", "c03"]},
            "p": {"grain": ["c01", "c02"], "time": None, "columns": ["c01", "c02", "c03"]},
        },
        joins={"p": {"t": {"left": "c01", "right": "c01", "card": "N:1"}}},
        pre_aggregates={},
        metrics={"m": metric},
        dimensions=dimensions or {
            "d_pay": Dimension("d_pay", {
                "id": "d_pay", "name_zh": "支付方式", "column": "p.c03",
                "filter_column": "p.c03", "join_path": ["t -> p"],
            }),
        },
        policies={}, time_range=("2020-01-01", "2030-01-01"),
        binding={"tables": {}}, raw={},
    )


def test_sum_rejects_one_to_many_dimension():
    metric = Metric("m", {
        "name_zh": "金额", "type": "aggregate", "fact": "t",
        "expr": "SUM(t.c02)", "supported_grain": ["none", "d_pay"],
    })
    plan = QueryPlan(metric="m", dimensions=["d_pay"])
    with pytest.raises(PipelineError):
        prepare(plan, _sem(metric))


def test_count_distinct_allows_one_to_many_dimension():
    metric = Metric("m", {
        "name_zh": "订单数", "type": "aggregate", "fact": "t",
        "expr": "COUNT(DISTINCT t.c01)", "supported_grain": ["none", "d_pay"],
    })
    plan = QueryPlan(metric="m", dimensions=["d_pay"])
    compiled, _ = prepare(plan, _sem(metric))
    assert "JOIN" in compiled.sql


def test_ratio_allows_one_to_many_dimension_and_flags_fanout():
    metric = Metric("m", {
        "name_zh": "比率", "type": "ratio", "fact": "t",
        "ratio_grain": "fact", "ratio_key": "t.c01",
        "numerator": "COUNT(t.c02)", "denominator": "COUNT(DISTINCT t.c01)",
        "grain_numerator": "MAX(CASE WHEN t.c02 > 0 THEN 1 ELSE 0 END)",
        "grain_denominator": "1",
        "supported_grain": ["none", "d_pay"],
    })
    plan = QueryPlan(metric="m", dimensions=["d_pay"])
    compiled, _ = prepare(plan, _sem(metric))
    assert compiled.meta.get("fanout_used") is True


def test_scope_and_filter_are_not_duplicated():
    metric = Metric("m", {
        "name_zh": "金额", "type": "aggregate", "fact": "t",
        "expr": "SUM(t.c02)", "scope": {"t.c03": "delivered"},
        "supported_grain": ["none", "d_status"],
    })
    dims = {"d_status": Dimension("d_status", {
        "id": "d_status", "name_zh": "订单状态", "column": "t.c03",
        "filter_column": "t.c03",
    })}
    plan = QueryPlan(
        metric="m",
        filters={"d_status": FilterSpec(op="eq", value="delivered")},
    )
    compiled, _ = prepare(plan, _sem(metric, dims))
    assert compiled.params == ["delivered"]
    assert compiled.sql.count("?") == 1


def test_order_by_is_nulls_last():
    metric = Metric("m", {
        "name_zh": "金额", "type": "aggregate", "fact": "t",
        "expr": "SUM(t.c02)", "supported_grain": ["none"],
    })
    plan = QueryPlan(metric="m", order_by=[OrderSpec(field="value", direction="desc")])
    compiled, _ = prepare(plan, _sem(metric, {}))
    assert "NULLS LAST" in compiled.sql
