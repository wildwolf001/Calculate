from __future__ import annotations

from typing import Any, Iterable, Protocol


class AgentState(Protocol):
    seq: int
    phase: str

    def apply(self, event: Any) -> None: ...


def replay(state: AgentState, events: Iterable[Any]) -> AgentState:
    for event in events:
        state.apply(event)
    return state
