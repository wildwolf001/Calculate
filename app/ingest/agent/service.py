from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agent.budget import Budget
from app.agent.contracts import ToolContext
from app.agent.controller import AgentController
from app.agent.events import JsonlEventLog
from app.ingest.agent.decider import LLMDecider
from app.ingest.agent.spec import build_ingest_agent


def run_ingest_agent(upload_dir: Path, out_dir: Path, client: Any,
                     budget: Budget | None = None):
    """Run the ingest agent with an LLM decider and return the final state."""
    spec = build_ingest_agent(
        Path(upload_dir), Path(out_dir), LLMDecider(client), budget=budget,
    )
    events = JsonlEventLog(Path(out_dir) / "ingest" / "events.jsonl")
    ctx = ToolContext(data={"upload_dir": str(upload_dir)})
    return AgentController(spec, events, ctx=ctx).run()


def summarize_state(state: Any, out_dir: Path) -> dict[str, Any]:
    return {
        "phase": getattr(state, "phase", "unknown"),
        "tables": sorted(getattr(state, "tables", {}).keys()),
        "metrics": {
            metric_id: {
                "name": metric.get("name"),
                "expr": metric.get("expr"),
                "probe": metric.get("probe"),
            }
            for metric_id, metric in (getattr(state, "metrics", {}) or {}).items()
        },
        "out_dir": str(out_dir),
        "draft": str(Path(out_dir) / "ingest" / "draft.json"),
    }
