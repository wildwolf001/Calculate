from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.agent.budget import Budget
from app.agent.contracts import Decision, GateResult, ToolContext, ToolResult
from app.agent.controller import AgentController, AgentSpec
from app.agent.events import JsonlEventLog
from app.agent.registry import ToolRegistry


class AddTool:
    name = "add"
    description = "add a fact"
    input_schema = {"type": "object", "required": ["value"]}

    def run(self, args, ctx):
        return ToolResult(facts={"x": int(args["value"])}, summary="added")


class FailTool:
    name = "fail"
    description = "raises"
    input_schema = {}

    def run(self, args, ctx):
        raise RuntimeError("boom")


@dataclass
class State:
    seq: int = 0
    phase: str = "start"
    facts: dict = field(default_factory=dict)

    def apply(self, event):
        self.seq = event.seq
        if event.type == "tool.result":
            self.facts.update(event.payload.get("facts") or {})
            self.phase = "worked"
        elif event.type == "agent.completed":
            self.phase = "done"


class XGate:
    id = "x"
    blocking = True

    def check(self, state):
        return GateResult(gate_id=self.id, ok=state.facts.get("x") == 2)


def _spec(tmp_path, decisions, budget=None):
    registry = ToolRegistry()
    registry.register(AddTool())
    registry.register(FailTool())
    script = iter(decisions)
    events = JsonlEventLog(tmp_path / "events.jsonl")

    def decide(state, catalog, last):
        return next(script)

    spec = AgentSpec(
        name="test",
        initial_event=("agent.started", {"dataset": "d"}),
        new_state=State,
        apply_event=lambda state, event: state.apply(event),
        tools=registry,
        gates=[XGate()],
        decide=decide,
        budget=budget or Budget(max_steps=5),
        terminal=lambda state: state.phase == "done",
    )
    return spec, events


def test_agent_loop_tool_then_finalize(tmp_path):
    spec, events = _spec(tmp_path, [
        Decision(kind="tool", tool="add", args={"value": 2}),
        Decision(kind="finalize"),
    ])
    state = AgentController(spec, events).run()
    assert state.phase == "done"
    assert state.facts["x"] == 2
    types = [event.type for event in events.read()]
    assert "tool.result" in types
    assert "agent.completed" in types


def test_agent_budget_exhaustion(tmp_path):
    spec, events = _spec(
        tmp_path,
        [Decision(kind="tool", tool="add", args={"value": 1}) for _ in range(10)],
        budget=Budget(max_steps=2),
    )
    AgentController(spec, events).run()
    types = [event.type for event in events.read()]
    assert "agent.budget_exhausted" in types


def test_registry_reports_unknown_and_exception(tmp_path):
    registry = ToolRegistry()
    registry.register(FailTool())
    ctx = ToolContext()
    assert registry.run("missing", {}, ctx).ok is False
    result = registry.run("fail", {}, ctx)
    assert result.ok is False
    assert "boom" in result.error


def test_finalize_is_rejected_when_gate_fails(tmp_path):
    spec, events = _spec(tmp_path, [Decision(kind="finalize")])
    AgentController(spec, events).run()
    types = [event.type for event in events.read()]
    assert "agent.finalize_rejected" in types
    assert "agent.completed" not in types
