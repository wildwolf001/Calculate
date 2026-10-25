"""Structured JSON logging with basic redaction."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from app.config.settings import LOG_DIR

SENSITIVE = re.compile(
    r"^(?:api[_-]?key|authorization|secret|password|access[_-]?token|refresh[_-]?token)$",
    re.IGNORECASE,
)
REDACTED = "***"


def redact(value):
    if isinstance(value, dict):
        return {
            k: (REDACTED if SENSITIVE.match(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str = "agent") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_DIR / "agent.jsonl", encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    sh = logging.StreamHandler()
    sh.setFormatter(JsonFormatter())
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, message: str, **fields) -> None:
    logger.info(message, extra={"extra_fields": fields})


logger = get_logger()
