"""Narrow interfaces used by the controller.

The controller owns transitions; these objects only transform one bounded
input into one structured output. Each has a deterministic fallback so the
system runs without an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ParseResult:
    intent_type: str = "query"
    tier: str = "A"                     # A simple | B multi-step | C explanation
    slots: dict = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    subquestions: list[dict] = field(default_factory=list)
    explanation: str | None = None
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    lookup_dim: str | None = None
    rewritten_question: str | None = None
    interpretations: list[dict] = field(default_factory=list)
    preferred_interpretation: str | None = None
    ambiguity_note: str | None = None
    visualization: dict | None = None
    context_action: str = "new"  # new | inherit | refine | reference_result
    context_source: str | None = None
    context_confidence: float = 0.0


@dataclass
class GroundResult:
    bindings: list[dict] = field(default_factory=list)
    ambiguity: dict | None = None
    notes: list[str] = field(default_factory=list)


class IntentParser(Protocol):
    def parse(self, text: str, context: dict) -> ParseResult: ...


class ValueGrounder(Protocol):
    def ground(self, slots: dict, unresolved: list[str], context: dict) -> GroundResult: ...


class AnswerComposer(Protocol):
    def compose(self, context: dict) -> str: ...
