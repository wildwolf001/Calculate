"""Analysis and compound-turn events remain replayable."""
from __future__ import annotations

from datetime import datetime

from app.core.events import Event
from app.core.state import Status, reduce


def _events(*specs):
    return [
        Event(seq=index, session_id="s1", ts=datetime(2026, 1, 1),
              type=event_type, payload=payload)
        for index, (event_type, payload) in enumerate(specs, 1)
    ]


def test_analysis_events_reduce_to_completed_report():
    state = reduce(_events(
        ("turn.started", {"turn": 1, "kind": "analysis"}),
        ("analysis.requested", {"items": [{"id": "an1"}]}),
        ("analysis.completed", {"report": "# 报告", "summary": "完成"}),
    ))
    assert state.status is Status.DONE
    assert state.analysis == {
        "status": "done", "report": "# 报告", "summary": "完成",
    }


def test_subquestions_completed_restores_primary_context():
    state = reduce(_events(
        ("turn.started", {"turn": 1}),
        ("subquestions.completed", {
            "primary_slots": {"metric": "sales", "dimensions": []},
        }),
    ))
    assert state.status is Status.DONE
    assert state.slots.metric == "sales"
