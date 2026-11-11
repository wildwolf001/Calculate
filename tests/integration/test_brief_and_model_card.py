"""EC51E4 regression: schema brief, model card, type gate, context inheritance."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.context import apply_context_action
from app.core.interfaces import ParseResult
from app.datasets.model_card import validate_model_card
from app.datasets.registry import DatasetRegistry
from app.domain.query.metric import MetricSpec
from app.domain.recovery import classify_error
from app.domain.repair import build_repair
from app.domain.semantic.loader import load_semantic
from app.llm.semantic_brief import build_brief

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data" / "datasets" / "ds_11670cbd5274"

pytestmark = pytest.mark.skipif(
    not (DATASET / "semantic.yaml").exists(),
    reason="EC51E4 dataset is not available",
)

QUESTION_T7 = "延迟订单与准时订单的平均评分分别是多少？"
QUESTION_T8 = "在有评价的已送达订单中，延迟订单和准时订单的平均评分分别是多少"


@pytest.fixture(scope="module")
def ctx():
    return DatasetRegistry().get("ds_11670cbd5274")


def test_model_card_marks_review_score_as_measure(ctx):
    card = ctx.model_card
    assert card.get("tables"), "model card was not built"
    review = card["tables"]["t_006"]
    score = next(col for col in review["columns"] if col["name"] == "c03")
    assert score["label"] == "review_score"
    assert score["role"] == "measure"
    assert score["type"] == "integer"
    assert score["min"] == 1 and score["max"] == 5
    assert validate_model_card(card) == []


def test_brief_exposes_review_table_columns(ctx):
    brief = build_brief(ctx.model_card, QUESTION_T8)
    assert "- t_006" in brief
    assert "review_score" in brief
    assert "review_score integer measure" in brief
    assert len(brief) <= 6000
    assert "预算不足" not in brief


def test_avg_on_varchar_is_rejected_before_sql():
    from app.domain.semantic.loader import SemanticLayer
    from app.domain.semantic.loader import Metric

    sem = SemanticLayer(
        version=1, dataset="fixture",
        tables={"t_006": {"grain": ["c01"], "time": "c06",
                          "columns": ["c01", "c02", "c03"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {"fact": "t_006", "expr": "COUNT(*)"})},
        dimensions={}, policies={}, time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}}, capabilities={}, raw={}, time_axes=[],
    )
    with pytest.raises(Exception) as exc:
        MetricSpec.from_input(
            {"aggregation": "AVG", "operand": "t_006.c02", "fact": "t_006"},
            sem,
            type_of=lambda table, column: "varchar" if column == "c02" else "integer",
        )
    assert "需要数值列" in str(exc.value)


def test_type_mismatch_is_not_classified_as_unsupported_plan():
    class BinderException(Exception):
        pass

    exc = BinderException(
        "Binder Error: No function matches the given name and argument types "
        "'avg(VARCHAR)'"
    )
    code, _message = classify_error(exc)
    assert code == "TYPE_MISMATCH"


def test_new_metric_does_not_inherit_previous_time():
    result = ParseResult(
        intent_type="query",
        slots={"metric": "m_llm_平均评分", "time": None, "comparison": None},
        context_action="refine",
    )
    context = {
        "active_scope": {
            "metric": "m_004_c06_sum",
            "time": {"start": "2025-04-01", "end": "2025-05-01"},
            "comparison": {"label": "4月 vs 3月"},
            "dimensions": ["d_004_009_c03"],
            "filters": {},
        }
    }
    apply_context_action(result, context, QUESTION_T8)
    assert result.slots.get("time") is None
    assert result.slots.get("comparison") is None
    assert result.slots.get("dimensions") in (None, [])
    assert any("未沿用" in note for note in result.slots.get("notes") or [])


def test_anaphora_still_inherits_previous_time():
    result = ParseResult(
        intent_type="query",
        slots={"metric": None, "time": None, "comparison": None},
        context_action="refine",
    )
    context = {
        "active_scope": {
            "metric": "m_004_c06_sum",
            "time": {"start": "2025-04-01", "end": "2025-05-01"},
            "comparison": None,
            "dimensions": [],
            "filters": {},
        }
    }
    apply_context_action(result, context, "那上个月呢？")
    assert result.slots.get("metric") == "m_004_c06_sum"
    assert result.slots.get("time") == {"start": "2025-04-01", "end": "2025-05-01"}


def test_clarification_offers_review_score_column(ctx):
    sem = load_semantic(
        DATASET / "semantic.yaml", DATASET / "binding.yaml",
        catalog_path=DATASET / "catalog.json",
    )
    repair = build_repair(
        sem,
        {"metric": "derived_x", "metric_spec": {"fact": "t_006"}},
        unknown=["平均评分的具体字段列"],
        model_card=ctx.model_card,
    )
    assert any("review_score" in option for option in repair["options"])


def test_large_schema_brief_stays_within_budget():
    tables = {}
    for index in range(100):
        tables[f"t_{index:03d}"] = {
            "label": f"table_{index}",
            "role": "fact" if index == 0 else "dimension",
            "rows": 1000,
            "grain": ["c01"],
            "time": "c02",
            "columns": [
                {"name": "c01", "label": "id", "type": "varchar", "role": "pk"},
                {"name": "c02", "label": "created_at", "type": "datetime", "role": "time"},
                {"name": "c03", "label": "amount", "type": "decimal", "role": "measure"},
                {"name": "c04", "label": "name", "type": "varchar", "role": "dimension"},
            ],
        }
    card = {
        "dataset_id": "ds_large",
        "semantic_version": 1,
        "tables": tables,
        "metrics": [
            {"id": "m1", "name": "销售额", "type": "aggregate",
             "aggregation": "sum", "operand": "t_000.c03", "fact": "t_000"},
        ],
        "dimensions": [],
        "relations": [],
        "docs": [],
    }
    brief = build_brief(card, "销售额是多少")
    assert len(brief) <= 6000
    shown = [line.split()[1] for line in brief.splitlines() if line.startswith("- t_")]
    assert 1 <= len(shown) <= 8
    assert "t_000" in shown


def test_model_card_overrides_apply(ctx):
    from app.datasets.model_card import build_model_card

    overrides = {
        "tables": {
            "t_006": {
                "description": "评价表",
                "columns": {
                    "c03": {
                        "label": "评分",
                        "description": "1-5 分",
                        "unit": "score",
                        "role": "measure",
                    }
                },
            }
        }
    }
    card = build_model_card(
        dataset_id=ctx.dataset_id,
        sem=ctx.sem,
        catalog=ctx.catalog,
        profile=ctx.profile,
        overrides=overrides,
        documentation=ctx.documentation,
        data_version=ctx.data_version,
    )
    table = card["tables"]["t_006"]
    column = next(col for col in table["columns"] if col["name"] == "c03")
    assert table["description"] == "评价表"
    assert column["label"] == "评分"
    assert column["description"] == "1-5 分"
    assert column["unit"] == "score"


def test_field_map_and_humanizer_use_chinese_labels():
    from app.core.controller import Controller
    from app.domain.plan import QueryPlan

    ctx = DatasetRegistry().get("ds_b211d4c710d9")
    if "entity_count_t_001" not in ctx.sem.metrics:
        pytest.skip("entity metric is not available")
    controller = object.__new__(Controller)
    controller.ctx = ctx
    controller.sem = ctx.sem
    mapping = controller._field_map(QueryPlan(metric="entity_count_t_001"))
    by_field = {item["field"]: item for item in mapping}
    entity = next(
        item for item in mapping
        if item["technical"] == "customer_unique_id"
    )
    assert entity["label"] == "自然客户"
    assert by_field["t_003.c04"]["label"] == "下单时间"
    assert by_field["t_003.c01"]["label"] == "订单号"

    rendered = controller._humanize_fields(
        "按 customer_unique_id 去重，order_id 为订单号", mapping,
    )
    assert "customer_unique_id" not in rendered
    assert "自然客户" in rendered


def test_t8_pipeline_answers_average_rating(ctx, tmp_path):
    """T8 acceptance without the LLM: AVG(c03) by delayed/on-time groups."""
    import asyncio

    from app.core.controller import Controller
    from app.core.event_store import EventStore
    from app.core.interfaces import ParseResult
    from app.domain.query.metric import MetricSpec
    from app.domain.resolver import RuleGrounder
    from app.llm.compose_answer import TemplateComposer

    spec = MetricSpec.from_input(
        {"aggregation": "AVG", "operand": "t_006.c03", "fact": "t_006",
         "scope": {"t_003.c03": "delivered"}, "time_via": "t_006.c06",
         "label": "平均评分", "unit": "分"},
        ctx.sem,
    )

    class StubParser:
        def parse(self, text, context):
            return ParseResult(
                intent_type="query",
                slots={
                    "metric": spec.id,
                    "metric_spec": spec.model_dump(mode="json"),
                    "dimensions": ["d_doc_t_003_c07_c08"],
                    "time": None,
                },
            )

    controller = Controller(
        EventStore(tmp_path / "events"), ctx.sem, StubParser(),
        RuleGrounder(ctx.sem, ctx.dim_values_path), TemplateComposer(),
        ctx=ctx,
    )
    controller.warehouse = ctx.warehouse

    async def run():
        return [event async for event in controller.run_turn("t8", QUESTION_T8)]

    events = asyncio.run(run())
    rows = next(event.payload["rows"] for event in events if event.type == "sql.executed")
    values = {row[0]: row[1] for row in rows}
    assert values["准时"] == pytest.approx(4.0188, abs=1e-3)
    assert values["延迟"] == pytest.approx(1.9987, abs=1e-3)

    answer = next(event.payload["text"] for event in events
                  if event.type == "answer.composed")
    assert "4.02（原始值 4.018816）" in answer
    assert "2.00（原始值 1.998709）" in answer

    field_map = next(event.payload["field_map"] for event in events
                     if event.type == "sql.executed")
    by_field = {item["field"]: item for item in field_map}
    assert by_field["t_006.c03"]["label"] == "评分"
    assert by_field["t_006.c03"]["technical"] == "review_score"
    assert by_field["t_003.c07"]["label"] == "是否延迟"

    quality = next(event.payload["issues"] for event in events
                   if event.type == "checks.completed")
    notice = next(item for item in quality if item.get("id") == "DATA_QUALITY")
    assert "1176" in notice["detail"]
    assert "是否延迟" in notice["detail"]
    assert "2830" not in notice["detail"]
