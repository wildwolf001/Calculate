from __future__ import annotations

from datetime import date

from app.core.state import Slots
from app.domain.model.expr import AggExpr, CountDistinctExpr, RatioExpr
from app.domain.model.model import SemanticModel
from app.domain.policy import DenyAllPolicy, PolicySet
from app.domain.plan import FilterSpec, TimeRange
from app.domain.query.ir import QueryIR
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _layer() -> SemanticLayer:
    return SemanticLayer(
        version=3, dataset="demo",
        tables={
            "t": {"grain": ["c01"], "time": "c02",
                  "columns": ["c01", "c02", "c03", "c04"]},
            "d": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
        },
        joins={"t": {"d": {"left": "c03", "right": "c01", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "m_sum": Metric("m_sum", {
                "name_zh": "金额", "type": "aggregate", "fact": "t",
                "expr": "SUM(t.c04)", "grain": ["c01"], "time_via": "t.c02",
                "synonyms": ["销售额"],
            }),
            "m_count": Metric("m_count", {
                "name_zh": "订单数", "type": "aggregate", "fact": "t",
                "expr": "COUNT(DISTINCT t.c01)", "time_via": "t.c02",
            }),
            "m_ratio": Metric("m_ratio", {
                "name_zh": "比率", "type": "ratio", "fact": "t",
                "numerator": "COUNT(t.c01)",
                "denominator": "COUNT(DISTINCT t.c01)",
            }),
        },
        dimensions={
            "d_city": Dimension("d_city", {
                "id": "d_city", "name_zh": "城市", "column": "d.c02",
                "synonyms": ["城市"],
            }),
            "d_flag": Dimension("d_flag", {
                "id": "d_flag", "name_zh": "是否延迟", "kind": "derived",
                "requires": "t",
                "labels": {"true": "延迟", "false": "准时"},
            }),
        },
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
        time_axes=[{"ref": "t.c02", "label": "purchase_at", "name_zh": "下单时间"}],
    )


def test_model_from_layer_has_entities_relations_and_metrics():
    model = SemanticModel.from_layer(_layer())
    assert model.version == "3"
    assert set(model.tables) == {"t", "d"}
    assert model.entities["e_t"].key == "c01"
    assert model.relations[0].cardinality == "N:1"
    assert isinstance(model.metrics["m_sum"].expr, AggExpr)
    assert isinstance(model.metrics["m_count"].expr, CountDistinctExpr)
    assert isinstance(model.metrics["m_ratio"].expr, RatioExpr)
    assert model.measures["m_sum"].aggregation == "sum"
    assert model.measures["m_count"].aggregation == "count_distinct"
    assert model.time_axes["t.c02"].name == "下单时间"
    assert model.validate() == []


def test_query_ir_from_slots_is_lossless_enough():
    slots = Slots(
        metric="m_sum",
        dimensions=["d_city"],
        filters={"d_city": FilterSpec(op="eq", value="A")},
        time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
        share=True,
        limit_per_group=2,
    )
    ir = QueryIR.from_slots(slots)
    assert ir.metric == "m_sum"
    assert ir.dimensions == ("d_city",)
    assert ir.filters["d_city"]["value"] == "A"
    assert ir.time["start"] == "2025-05-01"
    assert ir.share is True
    assert ir.limit_per_group == 2
    assert ir.as_dict()["metric"] == "m_sum"


def test_policy_default_allow_and_deny():
    assert PolicySet().check("m_sum").allowed is True
    decision = DenyAllPolicy().check("m_sum")
    assert decision.allowed is False
    assert decision.code == "ACCESS_DENIED"


def test_draft_exposes_low_confidence_objects():
    from app.domain.model.draft import SemanticDraft

    model = SemanticModel.from_layer(_layer())
    draft = SemanticDraft.from_model(model, threshold=0.8)
    assert draft.requires_confirmation is True
    assert SemanticDraft.from_model(model, threshold=0.6).requires_confirmation is False
    assert any(issue.kind == "entity" for issue in draft.issues)
    assert draft.as_dict()["model_version"] == "3"
