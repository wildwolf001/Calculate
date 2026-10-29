from __future__ import annotations

from app.agent.contracts import ToolResult
from app.ingest.agent.decider import LLMDecider
from app.ingest.agent.state import IngestState


class FakeClient:
    available = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, system, user, max_tokens=1200, stage="llm"):
        self.calls.append((system, user, stage))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _state():
    state = IngestState()
    state.tables["orders"] = {"rows": 3, "columns": [{"name": "order_id"}]}
    return state


def test_decider_returns_validated_decision():
    client = FakeClient({"kind": "tool", "tool": "files.list", "args": {}})
    decision = LLMDecider(client)(_state(), [{"name": "files.list"}], None)
    assert decision.kind == "tool"
    assert decision.tool == "files.list"
    assert client.calls[0][2] == "ingest_decide"


def test_decider_failure_is_explicit():
    client = FakeClient(RuntimeError("boom"))
    decision = LLMDecider(client)(_state(), [], None)
    assert decision.kind == "fail"
    assert "boom" in decision.rationale


def test_decider_invalid_payload_is_fail():
    client = FakeClient({"kind": "tool"})
    decision = LLMDecider(client)(_state(), [], None)
    assert decision.kind == "fail"

    bad = FakeClient({"kind": "bogus"})
    assert LLMDecider(bad)(_state(), [], None).kind == "fail"


def test_decider_render_includes_tables_and_tools():
    client = FakeClient({"kind": "finalize"})
    LLMDecider(client)(_state(), [{"name": "table.profile"}], ToolResult(summary="ok"))
    user = client.calls[0][1]
    assert "orders" in user
    assert "table.profile" in user
