"""LLM-2: propose candidate values for an unresolved term.

The model may only *rank* candidates supplied by the catalog; it cannot invent
a new value. Returned values are intersected with the candidate set.
"""
from __future__ import annotations

import json

from app.llm.client import LLMClient, LLMError, LLMUnavailable

SYSTEM = (
    "你是实体值消歧器。只能从候选列表中选择，禁止创造新值。"
    "输出 JSON：{\"values\": [\"候选值\"], \"confidence\": 0.0-1.0}。"
    "如果没有把握，返回空数组。"
)


def propose_values(term: str, candidates: list[str], client: LLMClient | None = None,
                   max_suggestions: int = 3) -> list[str]:
    if not candidates:
        return []
    client = client or LLMClient()
    if not client.available:
        return []
    user = json.dumps({"term": term, "candidates": candidates}, ensure_ascii=False)
    try:
        data = client.complete_json(SYSTEM, user, max_tokens=600, stage="propose_values")
    except (LLMError, LLMUnavailable):
        return []
    proposed = data.get("values") or []
    allowed = set(candidates)
    return [v for v in proposed if v in allowed][:max_suggestions]
