"""Composition root: wire the real components once, for API / scripts / tests."""
from __future__ import annotations

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.session import SessionService
from app.core.trace import Tracer
from app.datasets.context import DatasetContext
from app.datasets.registry import DatasetRegistry
from app.domain.resolver import RuleGrounder
from app.llm.client import LLMClient
from app.llm.compose_answer import LLMComposer
from app.llm.parse_intent import LLMIntentParser


def build_controller(ctx: DatasetContext, store: EventStore,
                     with_trace: bool = True) -> Controller:
    client = LLMClient()
    parser = LLMIntentParser(ctx.sem, client=client,
                             dim_values_path=ctx.dim_values_path)
    grounder = RuleGrounder(ctx.sem, ctx.dim_values_path)
    composer = LLMComposer(client=client)
    return Controller(
        store,
        ctx.sem,
        parser,
        grounder,
        composer,
        tracer=Tracer(enabled=with_trace),
        ctx=ctx,
    )


def build_service(store: EventStore | None = None, with_trace: bool = True,
                  dataset_id: str | None = None,
                  registry: DatasetRegistry | None = None) -> SessionService:
    registry = registry or DatasetRegistry()
    store = store or EventStore()
    default_id = dataset_id or registry.default_id
    controllers: dict[str, tuple[tuple, Controller]] = {}

    def resolve(ds_id: str) -> Controller:
        ctx = registry.get(ds_id)
        key = (ctx.semantic_version, ctx.data_version, ctx.status)
        cached = controllers.get(ds_id)
        if cached is None or cached[0] != key:
            controllers[ds_id] = (key, build_controller(ctx, store, with_trace))
        return controllers[ds_id][1]

    return SessionService(
        resolve,
        store,
        default_dataset_id=default_id,
        context_resolver=registry.get,
        registry=registry,
        controller_invalidator=lambda ds_id: controllers.pop(ds_id, None),
    )


def llm_status() -> dict:
    client = LLMClient()
    return {
        "enabled": client.available,
        "model": client.model,
        "base_url": client.base_url,
    }
