"""Event-driven ingestion agent (skeleton).

The agent owns ingestion decisions; deterministic tools profile files, detect
grains and probe metric candidates. It emits events and writes a draft.
"""
from app.ingest.agent.decider import LLMDecider
from app.ingest.agent.spec import ScriptedDecider, build_ingest_agent

__all__ = ["LLMDecider", "ScriptedDecider", "build_ingest_agent"]
