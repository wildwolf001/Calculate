"""Anthropic-compatible LLM client (DeepSeek gateway by default).

The rest of the system treats this as an optional dependency: when no key is
configured, `available` is False and callers use deterministic fallbacks.
"""
from __future__ import annotations

import json
import re
import time

from app.config.settings import (
    LLM_API_KEY, LLM_BASE_URL, LLM_ENABLED, LLM_MODEL, LLM_TIMEOUT_SECONDS,
)
from app.core.logging import log_event, logger


class LLMUnavailable(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class LLMTruncated(LLMError):
    """The model stopped at max_tokens; the output must not be treated as complete."""


def extract_json(text: str) -> dict:
    """Extract the first JSON object; tolerates markdown fences and prose."""
    if not text:
        raise LLMError("LLM 返回空内容")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise LLMError(f"LLM 输出不含 JSON: {text[:200]}")
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM JSON 解析失败: {exc}") from exc


class LLMClient:
    def __init__(self, api_key: str = LLM_API_KEY, base_url: str = LLM_BASE_URL,
                 model: str = LLM_MODEL, timeout: float = LLM_TIMEOUT_SECONDS,
                 enabled: bool | None = None):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.enabled = LLM_ENABLED if enabled is None else enabled
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key) and self.enabled

    def _get(self):
        if not self.available:
            raise LLMUnavailable("未配置 ANTHROPIC_API_KEY")
        if self._client is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMUnavailable("未安装 anthropic SDK") from exc
            self._client = Anthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,  # controller owns retry policy
            )
        return self._client

    def complete_text(self, system: str, user: str, max_tokens: int = 1800,
                      stage: str = "llm") -> str:
        client = self._get()
        started = time.perf_counter()
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            log_event(logger, "llm.call_failed", stage=stage, model=self.model,
                      error=f"{type(exc).__name__}: {exc}",
                      duration_ms=int((time.perf_counter() - started) * 1000))
            raise LLMError(f"LLM 调用失败: {type(exc).__name__}: {exc}") from exc

        stop_reason = getattr(resp, "stop_reason", None)
        usage = getattr(resp, "usage", None)
        blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        text = "\n".join(blocks).strip()
        log_event(
            logger, "llm.call", stage=stage, model=self.model, max_tokens=max_tokens,
            stop_reason=stop_reason, text_len=len(text),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        if stop_reason == "max_tokens":
            raise LLMTruncated(f"模型输出达到 max_tokens={max_tokens}，内容被截断")
        if not text:
            raise LLMError("LLM 返回空文本")
        return text

    def complete_json(self, system: str, user: str, max_tokens: int = 1800,
                      stage: str = "llm") -> dict:
        return extract_json(self.complete_text(system, user, max_tokens=max_tokens, stage=stage))
