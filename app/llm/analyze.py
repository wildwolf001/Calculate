"""LLM-5: analysis commentary over deterministic section facts.

The SQL, tables, scope and caveats stay deterministic. The model only writes
core conclusions and per-section commentary; every number it emits is checked
against the provided context, and any failure falls back to the template report.
"""
from __future__ import annotations

import json
from pathlib import Path
from string import Template

from app.core.logging import log_event, logger
from app.llm.client import LLMClient, LLMError, LLMTruncated, LLMUnavailable
from app.llm.compose_answer import numbers_are_grounded

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class LLMAnalysisWriter:
    def __init__(self, client: LLMClient | None = None):
        self.client = client or LLMClient()
        self._system = Template(
            (PROMPT_DIR / "analysis_system.txt").read_text(encoding="utf-8")
        )
        self._user = Template(
            (PROMPT_DIR / "analysis_user.txt").read_text(encoding="utf-8")
        )

    def write(self, ctx: dict) -> dict | None:
        """Return {'core_conclusions', 'sections', 'watchouts'} or None."""
        if not self.client.available:
            return None
        system = self._system.substitute()
        user = self._user.substitute(
            context=json.dumps(ctx, ensure_ascii=False, default=str)
        )
        data = None
        # Nine sections plus the model's hidden reasoning routinely exceed 4k
        # tokens; start high enough that the common case is a single call.
        for budget in (6000, 7500):
            try:
                data = self.client.complete_json(
                    system, user, max_tokens=budget, stage="analyze"
                )
                break
            except LLMTruncated:
                continue
            except (LLMError, LLMUnavailable, ValueError) as exc:
                log_event(logger, "analysis.llm_failed",
                          error=f"{type(exc).__name__}: {exc}")
                return None
            except Exception as exc:  # never break the deterministic report
                log_event(logger, "analysis.llm_failed",
                          error=f"{type(exc).__name__}: {exc}")
                return None
        if not isinstance(data, dict):
            log_event(logger, "analysis.llm_failed", error="no JSON after retry")
            return None

        core = [
            str(x).strip() for x in (data.get("core_conclusions") or [])
            if str(x).strip()
        ]
        core = [x for x in core if numbers_are_grounded(x, ctx)][:4]

        sections: dict[str, str] = {}
        for item in data.get("sections") or []:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "").strip()
            text = str(item.get("commentary") or "").strip()
            if sid and text and numbers_are_grounded(text, ctx):
                sections[sid] = text

        watchouts = [
            str(x).strip() for x in (data.get("watchouts") or [])
            if str(x).strip()
        ]
        watchouts = [x for x in watchouts if numbers_are_grounded(x, ctx)][:4]

        if not core and not sections:
            log_event(logger, "analysis.llm_failed", error="all commentary ungrounded")
            return None
        return {
            "core_conclusions": core,
            "sections": sections,
            "watchouts": watchouts,
        }
