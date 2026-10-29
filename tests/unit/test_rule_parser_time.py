from __future__ import annotations

from datetime import date

from app.domain.semantic.loader import Metric, SemanticLayer
from app.llm.rule_parser import RuleParser


def _sem():
    return SemanticLayer(
        version=1, dataset="t",
        tables={"t": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03"]}},
        joins={}, pre_aggregates={},
        metrics={"m": Metric("m", {
            "name_zh": "商品销售额", "type": "aggregate", "fact": "t",
            "expr": "SUM(t.c03)", "time_via": "t.c02",
            "supported_grain": ["none", "month"],
        })},
        dimensions={}, policies={},
        time_range=("2024-01-01", "2026-01-08"),
        binding={"tables": {}}, raw={"generic": True},
    )


def _parser(tmp_path):
    path = tmp_path / "dim_values.json"
    path.write_text("{}", encoding="utf-8")
    return RuleParser(_sem(), path)


def test_month_only_uses_latest_year_inside_coverage(tmp_path):
    result = _parser(tmp_path).parse("5月的商品销售额", {"slots": {}})
    assert result.slots["metric"] == "m"
    assert result.slots["time"] == {"start": "2025-05-01", "end": "2025-06-01"}


def test_explicit_out_of_range_month_is_kept(tmp_path):
    result = _parser(tmp_path).parse("2026年5月的商品销售额", {"slots": {}})
    assert result.slots["time"] == {"start": "2026-05-01", "end": "2026-06-01"}
