"""Inherited aggregate time must not break a dimensionless member lookup."""
from __future__ import annotations

from datetime import date

from app.core.state import Slots
from app.domain.plan import TimeRange
from app.domain.planner import member_list_plan
from app.domain.pipeline import prepare
from app.domain.semantic.loader import Dimension, SemanticLayer


def _sem() -> SemanticLayer:
    return SemanticLayer(
        version=1,
        dataset="列表测试",
        tables={"products": {"grain": ["id"], "time": None}},
        joins={},
        pre_aggregates={},
        metrics={},
        dimensions={
            "product_name": Dimension("product_name", {
                "name_zh": "商品名称",
                "column": "products.name",
                "filter_column": "products.name",
            })
        },
        policies={},
        time_range=("2025-01-01", "2025-07-01"),
        binding={"tables": {"products": {"name": "name"}}},
        capabilities={"member_list": True},
        raw={},
    )


def test_member_list_drops_unsupported_inherited_time():
    sem = _sem()
    slots = Slots(
        intent="lookup",
        dimensions=["product_name"],
        time=TimeRange(start=date(2025, 1, 1), end=date(2025, 7, 1)),
    )
    plan = member_list_plan("product_name", slots, sem)
    assert plan.time is None
    assert plan.degraded_reason
    compiled, issues = prepare(plan, sem)
    assert not [issue for issue in issues if issue.severity == "error"]
    assert "SELECT DISTINCT" in compiled.sql
