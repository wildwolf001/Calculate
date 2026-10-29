"""Session boundary: ids, per-session serialization, retrieval, export."""
from __future__ import annotations

import asyncio
import csv
import io
import uuid
from typing import Callable

from app.config.settings import DEFAULT_DATASET_ID
from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.events import Event
from app.core.state import SessionState
from app.datasets.registry import DatasetError
from app.domain.records import verify_record


class SessionService:
    """Session boundary. One controller per dataset, resolved per session.

    `controller` is kept for the single-dataset case (unit tests and the
    default service). Multi-dataset services pass a resolver instead.
    """

    def __init__(self, controller_or_resolver: Controller | Callable[[str], Controller],
                 store: EventStore,
                 default_dataset_id: str = DEFAULT_DATASET_ID,
                 context_resolver: Callable[[str], object] | None = None,
                 registry=None,
                 controller_invalidator: Callable[[str], None] | None = None):
        if isinstance(controller_or_resolver, Controller):
            single = controller_or_resolver
            self.controller = single
            self._resolve = lambda _dataset_id: single
        elif callable(controller_or_resolver):
            self.controller = None
            self._resolve = controller_or_resolver
        else:  # pragma: no cover - defensive
            raise TypeError("SessionService needs a Controller or a resolver")
        self.store = store
        self.default_dataset_id = default_dataset_id
        self._context_resolver = context_resolver
        self.registry = registry
        self._controller_invalidator = controller_invalidator
        self._locks: dict[str, asyncio.Lock] = {}

    def invalidate_dataset(self, dataset_id: str) -> None:
        if self._controller_invalidator is not None:
            self._controller_invalidator(dataset_id)

    def _lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def current_dataset_id(self) -> str:
        """Resolve the default at call time, not at process start.

        Datasets can be deleted or created while the server is running; a
        default captured at startup would point at a dataset that no longer
        exists (the chat then looks like it has no data source).
        """
        if self.registry is not None:
            # An empty registry means "no datasets yet"; do not fall back to a
            # startup id that may point at a deleted dataset.
            return getattr(self.registry, "default_id", "") or ""
        return self.default_dataset_id

    def controller_for(self, dataset_id: str | None = None) -> Controller:
        ds = dataset_id or self.current_dataset_id()
        if not ds:
            raise DatasetError("尚未导入数据源")
        return self._resolve(ds)

    def context(self, dataset_id: str | None = None):
        dataset_id = dataset_id or self.current_dataset_id()
        if not dataset_id:
            raise DatasetError("尚未导入数据源")
        if self._context_resolver is not None:
            return self._context_resolver(dataset_id)
        controller = self._resolve(dataset_id)
        return getattr(controller, "ctx", None)

    async def create(self, session_id: str | None = None,
                     dataset_id: str | None = None) -> str:
        sid = session_id or f"s-{uuid.uuid4().hex[:12]}"
        ds = dataset_id or self.current_dataset_id()
        if not ds:
            raise DatasetError("尚未导入数据源，请先在数据管理页上传数据")
        # Fail before writing an event if the dataset cannot be loaded.
        if self._context_resolver is not None:
            self._context_resolver(ds)
        if self.store.exists(sid):
            existing = self.store.replay(sid)
            if existing.dataset_id and existing.dataset_id != ds:
                raise DatasetError(
                    f"会话 {sid} 已绑定数据源 {existing.dataset_id}，不能切换为 {ds}"
                )
        else:
            self.store.append(sid, "session.created",
                              {"session_id": sid, "dataset_id": ds})
        return sid

    async def turn(self, session_id: str, text: str):
        async with self._lock(session_id):
            state = self.store.replay(session_id)
            controller = self._resolve(state.dataset_id)
            async for ev in controller.run_turn(session_id, text):
                yield ev

    async def analysis(self, session_id: str, options: dict | None = None):
        async with self._lock(session_id):
            state = self.store.replay(session_id)
            controller = self._resolve(state.dataset_id)
            async for event in controller.run_analysis(session_id, options):
                yield event

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
        state = self.store.replay(session_id)
        artifact = state.artifacts.get(result_id)
        if not artifact:
            raise KeyError("result_id 不存在")
        controller = self._resolve(state.dataset_id)
        return verify_record(artifact, controller.sem,
                             db_path=controller.warehouse)

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
