"""Durable semantic memory for multi-turn conversation.

The event log remains the source of truth. This module builds a *soft*
conversation projection (summary, entities, open questions) that helps the
parser and composer understand follow-ups. Execution scope remains typed and
is never taken from a summary.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.logging import log_event, logger


_SYSTEM = """你是对话记忆压缩器。你只负责把多轮对话压缩成下一轮可用的语义记忆。
只输出一个 JSON 对象，不要解释，不要写 SQL，不要编造用户没有说过的数字或结论。

必须保留：
- 用户当前目标和最近确认的口径；
- 当前讨论的实体/对象；
- 仍未解决的问题；
- 哪些指代已经被解决。

不要把上一轮的时间/过滤条件写成当前问题已经确定，除非用户明确这样说。"""

_USER = """已有记忆：
{previous}

最近对话：
{recent}

本轮用户：
{question}

本轮回答：
{answer}

本轮执行 scope 的确定性摘要：
{scope}

请输出：
{{
  "summary": "不超过300字的连续摘要",
  "user_goal": "用户当前目标，一句话",
  "active_topics": ["最多5个"],
  "open_questions": ["最多3个"],
  "confidence": 0.0
}}
"""


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _jsonable(value):
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class MemoryCompressor:
    """LLM-assisted semantic compression with a deterministic fallback."""

    def __init__(self, client=None, sem=None):
        self.client = client
        self.sem = sem

    @property
    def available(self) -> bool:
        return bool(self.client is not None and getattr(self.client, "available", False))

    def _available(self) -> bool:
        return bool(self.client is not None and getattr(self.client, "available", False))

    def _metric_name(self, plan) -> str:
        metric_id = getattr(plan, "metric", None) if plan is not None else None
        if not metric_id:
            return ""
        try:
            return self.sem.metric(metric_id).name
        except Exception:
            return str(metric_id)

    def _dimension_name(self, dim: str) -> str:
        try:
            return str(self.sem.dimension(dim).raw.get("name_zh") or dim)
        except Exception:
            return str(dim)

    def _filter_digest(self, plan) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dim, spec in (getattr(plan, "filters", None) or {}).items():
            value = getattr(spec, "value", spec)
            out[self._dimension_name(dim)] = _jsonable(value)
        return out

    def _deterministic(self, previous: dict | None, question: str,
                       answer: str, plan, artifact) -> dict:
        metric = self._metric_name(plan)
        dimensions = [
            self._dimension_name(dim)
            for dim in (getattr(plan, "dimensions", None) or [])
        ]
        filters = self._filter_digest(plan)
        time = None
        raw_time = getattr(plan, "time", None) if plan is not None else None
        if raw_time is not None:
            time = _jsonable(raw_time)
        entities = []
        for name, value in filters.items():
            entities.append({"type": name, "value": _short(str(value), 80)})
        rows = (
            list(artifact.get("rows") or [])
            if isinstance(artifact, dict)
            else list(getattr(artifact, "rows", None) or [])
        )
        if rows and dimensions:
            entities.append({
                "type": dimensions[0],
                "value": _short(str(rows[0][0]), 80),
            })
        scope = {
            "metric": getattr(plan, "metric", None),
            "metric_name": metric,
            "dimensions": dimensions,
            "time": time,
            "filters": filters,
        }
        pieces = []
        if previous and previous.get("summary"):
            pieces.append(str(previous["summary"])[:600])
        current = (
            f"用户本轮问：{_short(question, 180)}。"
            f"指标：{metric or '未指定'}；维度：{'、'.join(dimensions) or '无'}；"
            f"筛选：{json.dumps(filters, ensure_ascii=False) if filters else '无'}。"
        )
        if answer:
            current += f"回答摘要：{_short(answer, 220)}"
        pieces.append(current)
        summary = " ".join(pieces)
        return {
            "version": 1,
            "summary": summary[:1200],
            "user_goal": _short(question, 180),
            "active_topics": list(dict.fromkeys(
                [x for x in [metric, *dimensions, *filters.keys()] if x]
            ))[:5],
            "entities": entities[:8],
            "open_questions": [],
            "scope_digest": scope,
            "source": "deterministic",
            "confidence": 0.55,
        }

    def compress(self, *, previous: dict | None, question: str, answer: str,
                 plan, artifact, recent_messages: list[dict],
                 turn: int = 0, pending: dict | None = None,
                 allow_llm: bool = True) -> dict:
        seed = self._deterministic(previous, question, answer, plan, artifact)
        if not allow_llm or not self.available:
            return seed
        # Compress with the model when the conversation is already longer than
        # a couple of turns. The first turn needs no summary; later turns do.
        if turn < 2:
            return seed
        recent = [
            {"role": m.get("role"), "text": _short(m.get("text", ""), 240)}
            for m in recent_messages[-6:]
        ]
        try:
            data = self.client.complete_json(
                _SYSTEM,
                _USER.format(
                    previous=json.dumps(
                        {k: (previous or {}).get(k) for k in
                         ("summary", "user_goal", "active_topics", "open_questions")},
                        ensure_ascii=False,
                    ),
                    recent=json.dumps(recent, ensure_ascii=False),
                    question=_short(question, 500),
                    answer=_short(answer, 900),
                    scope=json.dumps(seed["scope_digest"], ensure_ascii=False),
                ),
                max_tokens=900,
                stage="compress_memory",
            )
        except Exception as exc:
            log_event(
                logger, "memory.compress_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            return seed
        if not isinstance(data, dict):
            return seed
        summary = str(data.get("summary") or "").strip()
        if not summary:
            return seed
        topics = data.get("active_topics") or []
        open_questions = data.get("open_questions") or []
        if not isinstance(topics, list):
            topics = [topics]
        if not isinstance(open_questions, list):
            open_questions = [open_questions]
        seed.update({
            "summary": _short(summary, 1200),
            "user_goal": _short(data.get("user_goal") or question, 180),
            "active_topics": [
                _short(x, 40) for x in topics if str(x).strip()
            ][:5],
            "open_questions": [
                _short(x, 120) for x in open_questions if str(x).strip()
            ][:3],
            "source": "llm",
            "confidence": max(0.0, min(1.0, float(data.get("confidence") or 0.7))),
        })
        if pending:
            seed["open_questions"] = list(dict.fromkeys(
                seed["open_questions"] + [_short(str(pending.get("message") or ""), 120)]
            ))[:3]
        return seed
