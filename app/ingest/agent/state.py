from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestState:
    seq: int = 0
    phase: str = "start"
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    pending_human: dict[str, Any] | None = None

    def apply(self, event: Any) -> None:
        self.seq = event.seq
        payload = event.payload or {}
        if event.type == "tool.result":
            name = payload.get("name")
            facts = payload.get("facts") or {}
            if name == "files.list":
                for item in facts.get("files") or []:
                    self.files[item["name"]] = item
            elif name == "table.profile":
                table = facts.get("table") or {}
                if table.get("name"):
                    self.tables[table["name"]] = table
            elif name == "grain.detect":
                table = self.tables.get(facts.get("table"))
                if table is not None:
                    table["grain"] = list(facts.get("grain") or [])
                    table["grain_unique"] = bool(facts.get("unique"))
                    table["grain_duplicates"] = facts.get("duplicates")
            elif name in {"metric.propose", "ratio.propose"}:
                metric = facts.get("metric") or {}
                if metric.get("id"):
                    self.metrics[metric["id"]] = metric
            elif name == "metric.probe":
                metric = self.metrics.get(facts.get("metric_id"))
                if metric is not None:
                    metric["probe"] = {
                        "value": facts.get("value"),
                        "rows": facts.get("rows"),
                        "ok": facts.get("value") is not None,
                    }
        elif event.type == "human.requested":
            self.pending_human = payload
        elif event.type == "agent.completed":
            self.phase = "done"
        elif event.type == "agent.finalize_rejected":
            self.phase = "needs_review"
        elif event.type == "agent.budget_exhausted":
            self.phase = "partial"
