"""Composition root: wire the real components once, for API / scripts / tests."""
from __future__ import annotations

from app.core.controller import Controller
from app.core.event_store import EventStore
from app.core.session import SessionService
from app.core.trace import Tracer
from app.domain.resolver import RuleGrounder
from app.domain.semantic.loader import load_semantic
from app.llm.analyze import LLMAnalysisWriter
from app.llm.client import LLMClient
from app.llm.compose_answer import LLMComposer
from app.llm.parse_intent import LLMIntentParser


def build_service(store: EventStore | None = None, with_trace: bool = True) -> SessionService:
    sem = load_semantic()
    client = LLMClient()
    parser = LLMIntentParser(sem, client=client)
    grounder = RuleGrounder(sem)
    composer = LLMComposer(client=client)
    controller = Controller(
        store or EventStore(),
        sem,
        parser,
        grounder,
        composer,
        tracer=Tracer(enabled=with_trace),
        analysis_writer=LLMAnalysisWriter(client=client),
    )
    return SessionService(controller, controller.store)


def llm_status() -> dict:
    client = LLMClient()
    return {
        "enabled": client.available,
        "model": client.model,
        "base_url": client.base_url,
    }
