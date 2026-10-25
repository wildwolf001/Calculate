"""Minimal span tracing. One JSONL file per day, one span per line.

Span trees are reconstructed by (trace_id, span_id, parent_id); we do not
introduce an OpenTelemetry dependency for a local demo.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app.config.settings import TRACE_DIR


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def new_span_id() -> str:
    return uuid.uuid4().hex[:12]


class Tracer:
    def __init__(self, directory: Path = TRACE_DIR, enabled: bool = True):
        self.dir = directory
        self.enabled = enabled
        if enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _write(self, record: dict) -> None:
        if not self.enabled:
            return
        path = self.dir / f"{datetime.now():%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    @contextmanager
    def span(self, name: str, trace_id: str, parent_id: str | None = None,
             **attrs: Any) -> Iterator[dict]:
        span_id = new_span_id()
        started = time.perf_counter()
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_id": parent_id,
            "name": name,
            "attrs": attrs,
            "status": "ok",
        }
        try:
            yield record
        except Exception as exc:
            record["status"] = "error"
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
            self._write(record)


tracer = Tracer()
