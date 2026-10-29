from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Issue(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warn", "error"] = "warn"
    evidence: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool = True
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    error: str | None = None


class Decision(BaseModel):
    kind: Literal["tool", "ask_human", "finalize", "fail"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    question: str | None = None
    options: list[str] = Field(default_factory=list)
    rationale: str = ""
    tokens: int = 0


class GateResult(BaseModel):
    gate_id: str
    ok: bool
    blocking: bool = True
    evidence: dict[str, Any] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)


@dataclass
class ToolContext:
    dataset_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    state: Any = None


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class Gate(Protocol):
    id: str
    blocking: bool

    def check(self, state: Any) -> GateResult: ...
