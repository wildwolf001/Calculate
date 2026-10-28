"""Analysis report tables format text columns independently from metric units."""
from __future__ import annotations

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.domain.executor import ResultSet
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


class _Parser:
    def parse(self, text, context):
        raise AssertionError("not used")


class _Grounder:
    def ground(self, slots, unresolved, context):
        raise AssertionError("not used")


class _Composer:
    def compose(self, context):
        return ""


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="测试",
        tables={"orders": {"grain": ["id"]}},
        joins={},
        pre_aggregates={},
        metrics={
            "late_rate": Metric("late_rate", {
                "name_zh": "延迟率",
                "type": "ratio",
                "fact": "orders",
                "unit": "%",
                "supported_grain": ["none", "seller"],
            })
        },
        dimensions={
            "seller": Dimension("seller", {"name_zh": "商家名称"}),
        },
        policies={},
        time_range=("2025-01-01", "2025-02-01"),
        binding={"tables": {}},
        capabilities={},
        raw={},
    )


def test_text_dimension_is_not_formatted_as_percent(tmp_path):
    sem = _sem()
    controller = Controller(
        EventStore(tmp_path), sem, _Parser(), _Grounder(), _Composer()
    )
    artifact = ResultSet(
        result_id="r1",
        columns=["seller", "numerator", "denominator", "value", "order_count"],
        column_types=["VARCHAR", "BIGINT", "BIGINT", "DOUBLE", "BIGINT"],
        rows=[["北京食品饮料商家021", 54, 59, 54 / 59, 59]],
        row_count=1,
        truncated=False,
        sql="SELECT 1 LIMIT 1",
        params=[],
        fingerprint="fp",
        duration_ms=1,
        meta={},
    )
    entry = {
        "id": "an1",
        "section": "履约",
        "title": "商家延迟率",
        "plan": QueryPlan(
            metric="late_rate", dimensions=["seller"], limit=10
        ),
        "artifact": artifact,
        "issues": [],
        "insights": [],
    }
    report, _ = controller._build_analysis_report({}, {}, [entry], [], None)
    assert "北京食品饮料商家021" in report
    assert "91.53%" in report
