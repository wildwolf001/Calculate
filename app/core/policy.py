"""Failure policy helpers kept out of the controller's control flow.

Rules (ARCHITECTURE.md §2.6):
- LLM timeout/5xx: retry once;
- invalid structured output: retry once with the error attached;
- ambiguity: never guess, ask once;
- unsupported metric x dimension: refuse with an explanation;
- validation failure: no conclusion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from app.config.settings import LLM_MAX_RETRIES

T = TypeVar("T")


@dataclass
class RetryDecision:
    should_retry: bool
    attempt: int
    reason: str


def decide_retry(attempt: int, error: Exception | None = None,
                 max_retry: int = LLM_MAX_RETRIES) -> RetryDecision:
    return RetryDecision(
        should_retry=attempt < max_retry,
        attempt=attempt + 1,
        reason=f"{type(error).__name__}: {error}" if error else "unknown",
    )


def with_retry(fn: Callable[[], T], max_retry: int = LLM_MAX_RETRIES,
               on_retry: Callable[[int, Exception], None] | None = None) -> T:
    last: Exception | None = None
    for attempt in range(max_retry + 1):
        try:
            return fn()
        except Exception as exc:  # caller decides which exceptions are retryable
            last = exc
            if attempt >= max_retry:
                break
            if on_retry:
                on_retry(attempt + 1, exc)
    assert last is not None
    raise last
