"""Append-only JSONL event store, one file per session.

- append is atomic under a per-session lock;
- seq is assigned by the store, never by callers;
- load() tolerates a truncated last line (crash during write) but reports it;
- replay() is `reduce(load())` — no model, no IO inside the reducer.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from app.config.settings import SESSIONS_DIR
from app.core.events import Event
from app.core.state import SessionState, reduce

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_ROOT = SESSIONS_DIR


class EventStoreError(RuntimeError):
    pass


class EventStore:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._last_seq: dict[str, int] = {}

    # ---------- internals ----------
    def _lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            if session_id not in self._locks:
                self._locks[session_id] = threading.Lock()
            return self._locks[session_id]

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID_RE.match(session_id):
            raise EventStoreError(f"非法 session_id: {session_id!r}")
        return self.root / f"{session_id}.jsonl"

    def delete(self, session_id: str) -> bool:
        """Remove a session log. Returns False when it does not exist."""
        path = self._path(session_id)
        with self._lock(session_id):
            if not path.exists():
                return False
            path.unlink()
            self._last_seq.pop(session_id, None)
            return True

    def _scan_last_seq(self, path: Path) -> int:
        if not path.exists():
            return 0
        last = 0
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = int(json.loads(line)["seq"])
                except Exception:
                    continue
        return last

    # ---------- public ----------
    def append(self, session_id: str, event_type: str, payload: dict | None = None,
               caused_by: str | None = None, trace_id: str | None = None) -> Event:
        path = self._path(session_id)
        with self._lock(session_id):
            if session_id not in self._last_seq:
                self._last_seq[session_id] = self._scan_last_seq(path)
            seq = self._last_seq[session_id] + 1
            ev = Event(
                seq=seq,
                session_id=session_id,
                ts=datetime.now(),
                type=event_type,
                payload=payload or {},
                caused_by=caused_by,
                trace_id=trace_id,
            )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._last_seq[session_id] = seq
            return ev

    def append_many(self, session_id: str, events: list[tuple[str, dict]],
                    trace_id: str | None = None) -> list[Event]:
        return [self.append(session_id, t, p, trace_id=trace_id) for t, p in events]

    def load(self, session_id: str) -> list[Event]:
        path = self._path(session_id)
        if not path.exists():
            return []
        events: list[Event] = []
        with self._lock(session_id):
            raw = path.read_text(encoding="utf-8").splitlines()
        lines = [(i, line) for i, line in enumerate(raw, 1) if line.strip()]
        for idx, (lineno, line) in enumerate(lines):
            try:
                events.append(Event.from_dict(json.loads(line)))
            except Exception:
                # tolerate only the final line being truncated by a crash
                if idx == len(lines) - 1:
                    break
                raise EventStoreError(f"事件日志损坏: {path}:{lineno}")
        return events

    def replay(self, session_id: str) -> SessionState:
        return reduce(self.load(session_id), session_id=session_id)

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.jsonl"))

    def history(self, session_id: str) -> list[dict]:
        """Query records with enough provenance to inspect and re-run."""
        events = self.load(session_id)
        checks: dict[str, dict] = {}
        answers: dict[str, str] = {}
        for event in events:
            result_id = (event.payload or {}).get("result_id")
            if not result_id:
                continue
            if event.type == "checks.completed":
                checks[result_id] = event.payload
            elif event.type == "answer.composed":
                answers.setdefault(result_id, event.payload.get("text") or "")

        out = []
        for event in events:
            if event.type != "sql.executed":
                continue
            payload = event.payload or {}
            result_id = payload.get("result_id") or ""
            plan = payload.get("plan") or {}
            check = checks.get(result_id) or {}
            if payload.get("analysis_item"):
                kind = "analysis"
            elif payload.get("subquestion_id"):
                kind = "subquestion"
            elif payload.get("interpretation_id"):
                kind = "interpretation"
            else:
                kind = "query"
            turn = None
            if result_id.startswith("r") and "-" in result_id:
                head = result_id[1:].split("-", 1)[0]
                turn = int(head) if head.isdigit() else None
            out.append({
                "result_id": result_id,
                "seq": event.seq,
                "ts": event.ts.isoformat(timespec="seconds"),
                "turn": turn,
                "question": payload.get("question"),
                "kind": kind,
                "label": payload.get("interpretation_label"),
                "analysis_section": payload.get("analysis_section"),
                "metric": plan.get("metric"),
                "dimensions": plan.get("dimensions") or [],
                "time": plan.get("time"),
                "comparison": plan.get("comparison"),
                "filters": plan.get("filters") or {},
                "limit": plan.get("limit"),
                "degraded_reason": plan.get("degraded_reason"),
                "plan": plan,
                "sql": payload.get("sql"),
                "params": payload.get("params") or [],
                "fingerprint": payload.get("fingerprint"),
                "duration_ms": payload.get("duration_ms"),
                "truncated": bool(payload.get("truncated")),
                "warehouse_version": (payload.get("meta") or {}).get("warehouse_version"),
                "columns": payload.get("columns") or [],
                "row_count": payload.get("row_count"),
                "preview": (payload.get("rows") or [])[:3],
                "checks": check.get("issues") or [],
                "facts": check.get("facts") or {},
                "blocked": bool(check.get("blocked")),
                "status": "blocked" if check.get("blocked") else "ok",
                "answer": answers.get(result_id),
            })
        return out
