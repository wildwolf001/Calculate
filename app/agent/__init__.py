"""Shared agent kernel: loop, contracts, registry, events, budget.

The kernel knows nothing about chat or ingestion. Domain agents provide a
state reducer, tools, gates and a decide callable.
"""
from app.agent.contracts import Decision, GateResult, Issue, Tool, ToolContext, ToolResult
from app.agent.controller import AgentController, AgentSpec
from app.agent.events import AgentEvent, JsonlEventLog
from app.agent.registry import ToolRegistry

__all__ = [
    "AgentController", "AgentSpec", "AgentEvent", "Decision", "GateResult",
    "Issue", "JsonlEventLog", "Tool", "ToolContext", "ToolRegistry", "ToolResult",
]
