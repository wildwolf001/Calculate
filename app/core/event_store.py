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
        """Query records: provenance + a reference to the result snapshot.

        Full rows stay in the `sql.executed` event and are fetched by
        result_id; this projection carries a small preview plus everything
        needed to review and re-run the query.
        """
        events = self.load(session_id)
        checks: dict[str, dict] = {}
        answers: dict[str, str] = {}
        for ev in events:
            rid = (ev.payload or {}).get("result_id")
            if not rid:
                continue
            if ev.type == "checks.completed":
                checks[rid] = ev.payload
            elif ev.type == "answer.composed":
                answers.setdefault(rid, ev.payload.get("text") or "")

        out = []
        for ev in events:
            if ev.type != "sql.executed":
                continue
            p = ev.payload or {}
            rid = p.get("result_id") or ""
            plan = p.get("plan") or {}
            check = checks.get(rid) or {}
            turn = None
            if rid.startswith("r") and "-" in rid:
                head = rid[1:].split("-", 1)[0]
                turn = int(head) if head.isdigit() else None
            if p.get("analysis_item"):
                kind = "analysis"
            elif p.get("subquestion_id"):
                kind = "subquestion"
            elif p.get("interpretation_id"):
                kind = "interpretation"
            else:
                kind = "query"
            out.append({
                "result_id": rid,
                "seq": ev.seq,
                "ts": ev.ts.isoformat(timespec="seconds"),
                "turn": turn,
                "question": p.get("question"),
                "kind": kind,
                "label": p.get("interpretation_label"),
                "analysis_section": p.get("analysis_section"),
                # plan / scope
                "metric": plan.get("metric"),
                "dimensions": plan.get("dimensions") or [],
                "time": plan.get("time"),
                "comparison": plan.get("comparison"),
                "filters": plan.get("filters") or {},
                "limit": plan.get("limit"),
                "degraded_reason": plan.get("degraded_reason"),
                "plan": plan,
                # execution provenance
                "sql": p.get("sql"),
                "params": p.get("params") or [],
                "fingerprint": p.get("fingerprint"),
                "duration_ms": p.get("duration_ms"),
                "truncated": bool(p.get("truncated")),
                "warehouse_version": (p.get("meta") or {}).get("warehouse_version"),
                # result reference + preview
                "columns": p.get("columns") or [],
                "row_count": p.get("row_count"),
                "preview": (p.get("rows") or [])[:3],
                # checks and answer
                "checks": check.get("issues") or [],
                "facts": check.get("facts") or {},
                "blocked": bool(check.get("blocked")),
                "status": "blocked" if check.get("blocked") else "ok",
                "answer": answers.get(rid),
            })
        return out
