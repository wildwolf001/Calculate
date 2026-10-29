from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HumanQuestion:
    id: str
    question: str
    options: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
