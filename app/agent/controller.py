from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.agent.budget import Budget
from app.agent.contracts import Decision, Gate, GateResult, ToolContext, ToolResult
from app.agent.events import AgentEvent, JsonlEventLog
from app.agent.registry import ToolRegistry


@dataclass
class AgentSpec:
    name: str
    initial_event: tuple[str, dict[str, Any]]
    new_state: Callable[[], Any]
    apply_event: Callable[[Any, AgentEvent], None]
    tools: ToolRegistry
    gates: list[Gate]
    decide: Callable[[Any, list[dict[str, Any]], ToolResult | None], Decision]
    budget: Budget = field(default_factory=Budget)
    terminal: Callable[[Any], bool] = lambda state: False
    on_finalize: Callable[[Any], None] | None = None


class AgentController:
    def __init__(self, spec: AgentSpec, events: JsonlEventLog,
                 ctx: ToolContext | None = None):
        self.spec = spec
        self.events = events
        self.ctx = ctx or ToolContext()

    def _state(self) -> Any:
        state = self.spec.new_state()
        for event in self.events.read():
            self.spec.apply_event(state, event)
        self.ctx.state = state
        return state

    def gates(self, state: Any) -> list[GateResult]:
        return [gate.check(state) for gate in self.spec.gates]

    def run(self) -> Any:
        if self.events.last_seq == 0:
            event_type, payload = self.spec.initial_event
            self.events.append(event_type, payload)
        state = self._state()
        last_result: ToolResult | None = None
        while not self.spec.terminal(state):
            exhausted = self.spec.budget.exceeded()
            if exhausted:
                self.events.append("agent.budget_exhausted", {"reason": exhausted})
                break
            decision = self.spec.decide(state, self.spec.tools.catalog(), last_result)
            self.events.append("agent.decision", decision.model_dump())
            if decision.kind == "tool":
                if not decision.tool:
                    self.events.append("agent.failed", {"reason": "tool decision without name"})
                    break
                last_result = self.spec.tools.run(decision.tool, decision.args, self.ctx)
                self.events.append(
                    "tool.result",
                    {"name": decision.tool, **last_result.model_dump()},
                )
                self.spec.budget.consume_step(tokens=decision.tokens)
                state = self._state()
                continue
            if decision.kind == "ask_human":
                self.events.append("human.requested", {
                    "question": decision.question,
                    "options": decision.options,
                })
                break
            if decision.kind == "finalize":
                results = self.gates(state)
                blocking = [result for result in results if not result.ok and result.blocking]
                if blocking:
                    self.events.append("agent.finalize_rejected", {
                        "gates": [result.model_dump() for result in blocking],
                    })
                    break
                if self.spec.on_finalize:
                    self.spec.on_finalize(state)
                self.events.append("agent.completed", {"agent": self.spec.name})
                state = self._state()
                break
            self.events.append("agent.failed", {"reason": decision.rationale or "fail"})
            break
        return state
