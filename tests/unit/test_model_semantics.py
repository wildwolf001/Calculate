from __future__ import annotations

from app.domain.model.expr import AggExpr, CountDistinctExpr, RatioExpr
from app.domain.model.model import SemanticModel
from app.domain.model.sql import compile_metric_expr
from app.domain.model.types import MetricModel, RelationModel, TableModel
from app.domain.model.validate import validate_semantics


def _model() -> SemanticModel:
    return SemanticModel(
        dataset="x", version="1",
        tables={"t": TableModel("t", columns=("c1", "c2"), grain=("c1",))},
        entities={}, relations=(RelationModel("t", "t", "c1", "c1", cardinality="N:1"),),
        measures={}, dimensions={},
        metrics={
            "avg": MetricModel("avg", "平均", "t", AggExpr("AVG", "t.c2")),
            "ratio": MetricModel(
                "ratio", "比率", "t",
                RatioExpr(CountDistinctExpr("t.c1"), CountDistinctExpr("t.c2")),
            ),
        },
        time_axes={},
    )


def test_semantic_validation_flags_missing_grain_and_axis():
    issues = validate_semantics(_model())
    codes = {issue.code for issue in issues}
    assert "AVG_GRAIN_MISSING" in codes
    assert "RATIO_GRAIN_MISSING" in codes


def test_metric_expr_sql_primitives():
    class Sem:
        def column(self, ref: str) -> str:
            return ref.split(".", 1)[1]

    assert compile_metric_expr(AggExpr("SUM", "t.c2"), Sem()) == 'SUM("t"."c2")'
    assert compile_metric_expr(CountDistinctExpr("t.c1"), Sem()) == 'COUNT(DISTINCT "t"."c1")'
    ratio = RatioExpr(AggExpr("SUM", "t.c2"), CountDistinctExpr("t.c1"))
    assert compile_metric_expr(ratio, Sem()) == '(SUM("t"."c2")) / NULLIF((COUNT(DISTINCT "t"."c1")), 0)'


def test_draft_includes_semantic_issues():
    from app.domain.model.draft import SemanticDraft

    draft = SemanticDraft.from_model(_model(), threshold=0.6)
    assert any(issue.kind == "semantic" for issue in draft.issues)


def test_capability_refuses_ratio_without_grain():
    from app.core.state import Slots
    from app.domain.capability import assess_slots
    from app.domain.semantic.loader import Metric, SemanticLayer

    sem = SemanticLayer(
        version=1, dataset="x",
        tables={"t": {"grain": [], "time": None, "columns": ["c1", "c2"]}},
        joins={}, pre_aggregates={},
        metrics={"ratio": Metric("ratio", {
            "name_zh": "比率", "type": "ratio", "fact": "t",
            "numerator": "COUNT(t.c1)", "denominator": "COUNT(DISTINCT t.c1)",
        })},
        dimensions={}, policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )
    model = SemanticModel.from_layer(sem)
    report = assess_slots(Slots(metric="ratio"), sem, model=model)
    assert report.status == "refusable"
    assert report.refusal_payload()["code"] == "RATIO_GRAIN_MISSING"


def test_compiler_normalizes_simple_metric_expr():
    from app.domain.pipeline import prepare
    from app.domain.plan import QueryPlan
    from app.domain.semantic.loader import Metric, SemanticLayer

    sem = SemanticLayer(
        version=1, dataset="x",
        tables={"t": {"grain": ["c1"], "time": None, "columns": ["c1", "c2"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "金额", "type": "aggregate", "fact": "t",
            "expr": "SUM( t.c2 )",
        })},
        dimensions={}, policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )
    compiled, _ = prepare(QueryPlan(metric="m"), sem)
    assert 'SUM("t"."c2")' in compiled.sql


def test_ratio_table_mismatch_is_flagged():
    from app.domain.model.types import MetricModel, TableModel

    model = SemanticModel(
        dataset="x", version="1",
        tables={
            "t": TableModel("t", columns=("c1",), grain=("c1",)),
            "u": TableModel("u", columns=("c2",), grain=("c2",)),
        },
        entities={}, relations=(), measures={}, dimensions={},
        metrics={
            "r": MetricModel(
                "r", "跨表比率", "t",
                RatioExpr(AggExpr("SUM", "t.c1"), AggExpr("SUM", "u.c2")),
                grain=("c1",),
            ),
        },
        time_axes={},
    )
    codes = {issue.code for issue in validate_semantics(model)}
    assert "RATIO_TABLE_MISMATCH" in codes


def test_ratio_scope_literals_are_flagged():
    from app.domain.model.types import MetricModel, TableModel

    model = SemanticModel(
        dataset="x", version="1",
        tables={"t": TableModel("t", columns=("c1", "c2"), grain=("c1",))},
        entities={}, relations=(), measures={}, dimensions={},
        metrics={
            "r": MetricModel(
                "r", "条件比率", "t",
                RatioExpr(
                    AggExpr("COUNT", "CASE WHEN t.c1 = 'a' THEN 1 END"),
                    AggExpr("COUNT", "t.c1"),
                ),
                grain=("c1",),
            ),
        },
        time_axes={},
    )
    codes = {issue.code for issue in validate_semantics(model)}
    assert "RATIO_SCOPE_MISMATCH" in codes
