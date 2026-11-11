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
from app.domain.pipeline import check, execute_prepared, prepare
from app.domain.plan import FilterSpec, QueryPlan
from app.domain.presentation import display_spec
from app.domain.records import verify_record
from app.domain.visualization import recommend_chart


def _csv_cell(value):
    """Neutralise spreadsheet formula injection on export.

    A result cell is user data; Excel still evaluates any cell starting with
    `=`, `+`, `-` or `@`, so prefix those with a single quote.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


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

    def cancel(self, session_id: str) -> None:
        state = self.store.replay(session_id)
        if state.status.value in {"IDLE", "DONE", "FAILED", "AWAITING_CLARIFICATION"}:
            return
        controller = self._resolve(state.dataset_id)
        controller.cancel(session_id)

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

    def drill(self, session_id: str, result_id: str, action: str,
              dimension: str | None = None, value: str | None = None,
              next_dimension: str | None = None,
              limit: int | None = None) -> dict:
        """Apply a deterministic filter/group-by to an existing result.

        Drilling must not re-run the LLM parser: the value the user clicked is
        already exact, and rephrasing it can change the metric. This reuses the
        stored result's plan, applies one explicit operation, and executes it.
        """
        if action not in {"filter", "group_by"}:
            raise ValueError("不支持的 drill 操作")
        state = self.state(session_id)
        artifact = state.artifacts.get(result_id)
        if not artifact:
            raise KeyError("result_id 不存在")
        event = next(
            (
                item for item in reversed(self.events(session_id))
                if item.type == "sql.executed"
                and (item.payload or {}).get("result_id") == result_id
            ),
            None,
        )
        if event is None or not (event.payload or {}).get("plan"):
            raise KeyError("找不到该结果对应的查询计划")
        plan = QueryPlan.model_validate(event.payload["plan"])
        controller = self._resolve(state.dataset_id)
        sem = controller.sem
        if not dimension:
            raise ValueError("缺少下钻维度")
        if dimension not in sem.dimensions:
            raise ValueError(f"未知维度: {dimension}")
        if action == "filter":
            plan.dimensions = []
        else:
            if not next_dimension:
                raise ValueError("缺少下一层维度")
            if next_dimension not in sem.dimensions:
                raise ValueError(f"未知维度: {next_dimension}")
            plan.dimensions = [next_dimension]
        if not value:
            raise ValueError("缺少下钻值")
        plan.filters[dimension] = FilterSpec(op="eq", value=str(value))
        plan.compare = None
        plan.comparison = None
        plan.having = []
        plan.limit = max(1, min(int(limit or 100), 200))
        compiled, _issues = prepare(plan, sem)
        result = execute_prepared(compiled, db_path=controller.warehouse)
        fingerprint = compiled.fingerprint(
            int(sem.raw.get("compiler_version", 1)), sem.version,
        )
        result.fingerprint = fingerprint
        result.result_id = f"drill-{uuid.uuid4().hex[:12]}"
        checks, facts = check(
            plan, sem, result, db_path=controller.warehouse,
            quality=controller.quality,
            catalog=getattr(controller.ctx, "catalog", None),
        )
        display = display_spec(plan, sem, result)
        payload = result.as_dict()
        payload.update({
            "question": f"{dimension}={value}",
            "display": display,
            "chart": recommend_chart(plan, sem, display, result),
            "plan": plan.model_dump(mode="json"),
            "checks": [item.as_dict() for item in checks],
            "facts": facts,
            "field_map": controller._field_map(plan),
            "drill": {
                "action": action,
                "dimension": dimension,
                "value": value,
                "next_dimension": next_dimension,
            },
        })
        return payload

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
        writer.writerow([_csv_cell(c) for c in (artifact.get("columns") or [])])
        for row in artifact.get("rows") or []:
            writer.writerow([_csv_cell(value) for value in row])
        return f"{result_id}.csv", buf.getvalue()
