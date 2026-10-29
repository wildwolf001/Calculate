from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str

    @classmethod
    def load(cls, path: Path) -> "Prompt":
        text = Path(path).read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return cls(text=text, version=digest)
