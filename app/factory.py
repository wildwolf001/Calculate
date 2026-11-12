"""Composition root: wire the real components once, for API / scripts / tests."""
from __future__ import annotations

from app.config.settings import USE_SEMANTIC_BRIEF
from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.memory import MemoryCompressor
from app.core.session import SessionService
from app.core.trace import Tracer
from app.datasets.context import DatasetContext
from app.datasets.registry import DatasetRegistry
from app.domain.resolver import RuleGrounder
from app.llm.client import LLMClient
from app.llm.analyze import LLMAnalysisWriter
from app.llm.compose_answer import LLMComposer
from app.llm.parse_intent import LLMIntentParser
from app.llm.runtime_config import public_config


def build_controller(ctx: DatasetContext, store: EventStore,
                     with_trace: bool = True) -> Controller:
    client = LLMClient()
    parser = LLMIntentParser(
        ctx.sem,
        client=client,
        dim_values_path=ctx.dim_values_path,
        index=ctx.index,
        use_brief=USE_SEMANTIC_BRIEF,
        model_card=ctx.model_card,
    )
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
        analysis_writer=LLMAnalysisWriter(client=client),
        memory_compressor=MemoryCompressor(client=client, sem=ctx.sem),
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
    cfg = public_config()
    return {
        "enabled": cfg["enabled"],
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
    }
