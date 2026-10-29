from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.agent.budget import Budget
from app.agent.contracts import Decision, ToolContext
from app.agent.controller import AgentSpec
from app.agent.events import JsonlEventLog
from app.agent.registry import ToolRegistry
from app.ingest.agent.artifacts import IngestArtifacts
from app.ingest.agent.gates import GrainGate, MetricGate, TablesProfiledGate
from app.ingest.agent.state import IngestState
from app.ingest.agent.tools import (
    GrainDetectTool,
    ListFilesTool,
    MetricProbeTool,
    MetricProposeTool,
    RatioProposeTool,
    TableProfileTool,
)


@dataclass
class ScriptedDecider:
    decisions: Iterable[Decision]

    def __post_init__(self) -> None:
        self._iterator = iter(list(self.decisions))

    def __call__(self, state, catalog, last):
        return next(self._iterator)


def build_ingest_agent(upload_dir: Path, out_dir: Path,
                       decide: Callable[[Any, list[dict], Any], Decision],
                       budget: Budget | None = None) -> AgentSpec:
    upload_dir = Path(upload_dir)
    out_dir = Path(out_dir)
    registry = ToolRegistry()
    for tool in (
        ListFilesTool(),
        TableProfileTool(),
        GrainDetectTool(),
        MetricProposeTool(),
        MetricProbeTool(),
        RatioProposeTool(),
    ):
        registry.register(tool)
    artifacts = IngestArtifacts(out_dir)
    events = JsonlEventLog(out_dir / "ingest" / "events.jsonl")
    spec = AgentSpec(
        name="ingest",
        initial_event=("ingest.started", {"upload_dir": str(upload_dir)}),
        new_state=IngestState,
        apply_event=lambda state, event: state.apply(event),
        tools=registry,
        gates=[TablesProfiledGate(), GrainGate(), MetricGate()],
        decide=decide,
        budget=budget or Budget(max_steps=20, max_tokens=50_000, max_seconds=120),
        terminal=lambda state: state.phase in {"done", "needs_review", "partial"},
        on_finalize=lambda state: artifacts.write(state),
    )
    return spec
