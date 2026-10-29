from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    seq: int
    ts: datetime
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    caused_by: int | None = None
    trace_id: str | None = None


class JsonlEventLog:
    """Append-only JSONL event log with seq/last_seq and replay."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = max(last, int(json.loads(line).get("seq", 0)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        return last

    def append(self, event_type: str, payload: dict[str, Any] | None = None,
               caused_by: int | None = None) -> AgentEvent:
        event = AgentEvent(
            seq=self.last_seq + 1,
            ts=datetime.now(),
            type=event_type,
            payload=payload or {},
            caused_by=caused_by,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        return event

    def read(self, since: int = 0) -> Iterator[AgentEvent]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = AgentEvent.model_validate_json(line)
                except Exception:
                    continue
                if event.seq > since:
                    yield event
