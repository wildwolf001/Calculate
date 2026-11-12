"""Anthropic-compatible LLM client (DeepSeek gateway by default).

The rest of the system treats this as an optional dependency: when no key is
configured, `available` is False and callers use deterministic fallbacks.
"""
from __future__ import annotations

import json
import os
import re
import time

from app.config.settings import LLM_TIMEOUT_SECONDS
from app.core.logging import log_event, logger
from app.llm.runtime_config import PRESETS, load_config

_LLM_DISABLED = os.getenv("LLM_DISABLED", "").lower() in {"1", "true", "yes"}


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
        try:
            value, _ = json.JSONDecoder().raw_decode(text, start)
            if isinstance(value, dict):
                return value
            raise LLMError("LLM JSON top level must be an object")
        except json.JSONDecodeError:
            # Preserve the existing parser error when the first object is
            # genuinely incomplete or malformed.
            candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM JSON 解析失败: {exc}") from exc


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: float = LLM_TIMEOUT_SECONDS,
                 enabled: bool | None = None, provider: str | None = None):
        config = load_config()
        self.api_key = config["api_key"] if api_key is None else api_key
        self.base_url = config["base_url"] if base_url is None else base_url
        self.model = config["model"] if model is None else model
        self.provider = (provider or config["provider"]).lower()
        self.protocol = str(
            (PRESETS.get(self.provider) or {}).get("protocol")
            or config.get("protocol")
            or "anthropic"
        )
        self.timeout = timeout
        if enabled is None:
            enabled = bool(self.api_key) and not _LLM_DISABLED
        self.enabled = enabled
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key) and self.enabled

    def _get(self):
        if not self.available:
            raise LLMUnavailable("未配置模型 API Key")
        if self.protocol == "openai":
            raise LLMUnavailable("OpenAI 兼容协议不使用 Anthropic client")
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

    def _complete_openai(self, system: str, user: str, max_tokens: int,
                         stage: str) -> str:
        if not self.available:
            raise LLMUnavailable("未配置模型 API Key")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailable("未安装 httpx") from exc
        started = time.perf_counter()
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except Exception as exc:
            log_event(
                logger, "llm.call_failed", stage=stage, model=self.model,
                provider=self.provider,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise LLMError(f"LLM 调用失败: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:500]
            log_event(logger, "llm.call_failed", stage=stage, model=self.model,
                      provider=self.provider, status=resp.status_code,
                      error=detail)
            raise LLMError(f"LLM 网关返回 {resp.status_code}: {detail}")
        try:
            data = resp.json()
        except Exception as exc:
            raise LLMError(f"LLM 返回非 JSON: {resp.text[:300]}") from exc
        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict)
            )
        text = str(content or "").strip()
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason")
        log_event(
            logger, "llm.call", stage=stage, model=self.model,
            provider=self.provider, max_tokens=max_tokens,
            stop_reason=finish, text_len=len(text),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        if finish == "length":
            raise LLMTruncated(f"模型输出达到 max_tokens={max_tokens}，内容被截断")
        if not text:
            raise LLMError("LLM 返回空文本")
        return text

    def complete_text(self, system: str, user: str, max_tokens: int = 1800,
                      stage: str = "llm") -> str:
        if self.protocol == "openai":
            return self._complete_openai(system, user, max_tokens, stage)
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
                      provider=self.provider,
                      error=f"{type(exc).__name__}: {exc}",
                      duration_ms=int((time.perf_counter() - started) * 1000))
            raise LLMError(f"LLM 调用失败: {type(exc).__name__}: {exc}") from exc

        stop_reason = getattr(resp, "stop_reason", None)
        usage = getattr(resp, "usage", None)
        blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        text = "\n".join(blocks).strip()
        log_event(
            logger, "llm.call", stage=stage, model=self.model,
            provider=self.provider, max_tokens=max_tokens,
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
