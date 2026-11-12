"""Result-reference turns must keep the active grouped scope."""
from __future__ import annotations

from app.core.controller import Controller
from app.core.interfaces import ParseResult


def test_reference_focus_binds_previous_dimension_and_members():
    controller = object.__new__(Controller)
    result = ParseResult(
        intent_type="query",
        tier="A",
        confidence=0.9,
        slots={"metric": "sales", "dimensions": [], "time": None, "filters": {}},
        context_action="new",
    )
    context = {
        "focus": {
            "entity_dimension": "category",
            "values": ["数码家电", "家居生活"],
        },
        "active_scope": {
            "metric": "delay_rate",
            "dimensions": ["category"],
            "time": {"start": "2025-01-01", "end": "2025-07-01"},
            "filters": {},
        },
    }
    controller._apply_reference_focus(result, context, "其中销售额最高的是哪一个？")
    assert result.context_action == "reference_result"
    assert result.slots["dimensions"] == ["category"]
    assert result.slots["filters"]["category"]["op"] == "in"
    assert result.slots["filters"]["category"]["value"] == ["数码家电", "家居生活"]
