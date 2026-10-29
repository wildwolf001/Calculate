from __future__ import annotations

from app.domain.presentation import summarize_rows
from app.llm.compose_answer import TemplateComposer


ROWS = [
    ["广州", "办公椅20款", 10636.92],
    ["广州", "空气炸锅3款", 9000.0],
    ["上海", "跑步鞋18款", 7755.22],
    ["上海", "办公椅16款", 6000.0],
]


def test_summary_keeps_total_and_top_per_group():
    summary = summarize_rows(
        ROWS, ["city", "product", "value"], ["city", "product"], max_rows=2
    )
    assert summary["rows_total"] == 4
    assert summary["rows_truncated"] is True
    assert len(summary["rows"]) == 2
    assert summary["per_group_dimension"] == "city"
    assert [row[1] for row in summary["per_group_top"]] == ["办公椅20款", "跑步鞋18款"]
    assert "共 4 行" in summary["rows_note"]


def test_template_answer_uses_per_group_top_not_truncated_rows():
    summary = summarize_rows(
        ROWS, ["city", "product", "value"], ["city", "product"], max_rows=2
    )
    ctx = {
        "metric_name": "销售额",
        "metric_unit": "元",
        "dimension_labels": ["城市", "商品"],
        "columns": ["city", "product", "value"],
        "checks": [],
        "facts": {},
        **summary,
    }
    text = TemplateComposer().compose(ctx)
    assert "广州：办公椅20款" in text
    assert "上海：跑步鞋18款" in text
