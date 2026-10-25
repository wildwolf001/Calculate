"""Session boundary: ids, per-session serialization, retrieval, export."""
from __future__ import annotations

import asyncio
import csv
import io
import uuid

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.events import Event
from app.core.state import SessionState
from app.domain.records import verify_record


class SessionService:
    def __init__(self, controller: Controller, store: EventStore):
        self.controller = controller
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def create(self, session_id: str | None = None) -> str:
        sid = session_id or f"s-{uuid.uuid4().hex[:12]}"
        if not self.store.exists(sid):
            self.store.append(sid, "session.created", {"session_id": sid})
        return sid

    async def turn(self, session_id: str, text: str):
        async with self._lock(session_id):
            async for ev in self.controller.run_turn(session_id, text):
                yield ev

    async def analysis(self, session_id: str, options: dict | None = None):
        async with self._lock(session_id):
            async for ev in self.controller.run_analysis(session_id, options):
                yield ev

    def state(self, session_id: str) -> SessionState:
        return self.store.replay(session_id)

    def events(self, session_id: str) -> list[Event]:
        return self.store.load(session_id)

    def result(self, session_id: str, result_id: str) -> dict | None:
        state = self.state(session_id)
        return state.artifacts.get(result_id)

    def history(self, session_id: str) -> list[dict]:
        return self.store.history(session_id)

    def verify(self, session_id: str, result_id: str) -> dict:
        artifact = self.result(session_id, result_id)
        if not artifact:
            raise KeyError("result_id 不存在")
        return verify_record(artifact, self.controller.sem)

    def export_csv(self, session_id: str, result_id: str) -> tuple[str, str]:
        artifact = self.result(session_id, result_id)
        if not artifact:
            raise KeyError("result_id 不存在")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(artifact.get("columns") or [])
        for row in artifact.get("rows") or []:
            writer.writerow(row)
        return f"{result_id}.csv", buf.getvalue()
