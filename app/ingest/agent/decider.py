from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent.contracts import Decision, ToolResult

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system.txt"


class LLMDecider:
    """Ask the model for one decision per step; never let it write SQL."""

    def __init__(self, client: Any, system_prompt: str | None = None,
                 max_tokens: int = 1200):
        self.client = client
        self.system = system_prompt or PROMPT_PATH.read_text(encoding="utf-8")
        self.max_tokens = max_tokens

    def _render(self, state: Any, catalog: list[dict], last: ToolResult | None) -> str:
        tables = {
            name: {
                "rows": table.get("rows"),
                "grain": table.get("grain"),
                "grain_unique": table.get("grain_unique"),
                "columns": [
                    {"name": col.get("name"), "kind": col.get("kind")}
                    for col in (table.get("columns") or [])[:20]
                ],
            }
            for name, table in list(getattr(state, "tables", {}).items())[:12]
        }
        metrics = {
            mid: {
                "name": metric.get("name"),
                "expr": metric.get("expr"),
                "scope": metric.get("scope"),
                "probe": metric.get("probe"),
            }
            for mid, metric in list(getattr(state, "metrics", {}).items())[:20]
        }
        payload = {
            "phase": getattr(state, "phase", "start"),
            "files": list(getattr(state, "files", {}).keys())[:20],
            "tables": tables,
            "metrics": metrics,
            "tools": catalog,
            "last_result": last.model_dump() if last else None,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    def __call__(self, state: Any, catalog: list[dict],
                 last: ToolResult | None) -> Decision:
        if not getattr(self.client, "available", True):
            return Decision(kind="fail", rationale="LLM 不可用")
        try:
            data = self.client.complete_json(
                self.system, self._render(state, catalog, last),
                max_tokens=self.max_tokens, stage="ingest_decide",
            )
            decision = Decision.model_validate(data)
            if decision.kind == "tool" and not decision.tool:
                return Decision(kind="fail", rationale="tool decision without name")
            return decision
        except Exception as exc:
            return Decision(kind="fail", rationale=f"{type(exc).__name__}: {exc}")
