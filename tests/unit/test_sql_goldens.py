from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.domain.plan import CompareSpec, FilterSpec, OrderSpec, QueryPlan, TimeRange
from app.domain.pipeline import prepare
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer


def _sem():
    return SemanticLayer(
        version=1, dataset="t",
        tables={
            "t": {"grain": ["c01"], "time": "c02", "columns": ["c01", "c02", "c03", "c04"]},
            "d": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
        },
        joins={"t": {"d": {"left": "c03", "right": "c01", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "m": Metric("m", {
                "name_zh": "金额", "type": "aggregate", "fact": "t",
                "expr": "SUM(t.c04)", "time_via": "t.c02", "additive": True,
            }),
            "r": Metric("r", {
                "name_zh": "比率", "type": "ratio", "fact": "t",
                "ratio_grain": "fact", "ratio_key": "t.c01",
                "numerator": "COUNT(CASE WHEN t.c04 > 0 THEN 1 END)",
                "denominator": "COUNT(DISTINCT t.c01)",
                "grain_numerator": "MAX(CASE WHEN t.c04 > 0 THEN 1 ELSE 0 END)",
                "grain_denominator": "1",
            }),
        },
        dimensions={
            "d_city": Dimension("d_city", {"id": "d_city", "name_zh": "城市", "column": "d.c02"}),
            "d_flag": Dimension("d_flag", {"id": "d_flag", "name_zh": "标记", "column": "d.c01"}),
        },
        policies={}, time_range=("2024-01-01", "2025-12-31"),
        binding={"tables": {}}, raw={},
    )


def _plans():
    return {
        "aggregate": QueryPlan(
            metric="m", dimensions=["d_city"],
            time=TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1)),
            filters={"d_city": FilterSpec(op="eq", value="A")},
        ),
        "ratio": QueryPlan(metric="r", dimensions=["d_city"]),
        "per_group": QueryPlan(
            metric="m", dimensions=["d_city", "d_flag"],
            order_by=[OrderSpec(field="value", direction="desc")],
            limit=50, limit_per_group=1,
        ),
        "compare": QueryPlan(
            metric="m", dimensions=["d_city"],
            time=TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1)),
            compare=CompareSpec(),
        ),
        "share": QueryPlan(metric="m", dimensions=["d_city"], share=True),
    }


def test_sql_goldens():
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "sql_goldens.json")
        .read_text(encoding="utf-8")
    )
    for name, plan in _plans().items():
        compiled, _ = prepare(plan, _sem())
        assert compiled.sql == fixture[name]["sql"], f"SQL changed for {name}"
        assert [str(p) for p in compiled.params] == fixture[name]["params"], name
        assert compiled.columns == fixture[name]["columns"], name
