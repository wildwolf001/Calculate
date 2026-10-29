from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Budget:
    max_steps: int = 60
    max_tokens: int = 200_000
    max_seconds: float = 600.0
    steps: int = 0
    tokens: int = 0
    elapsed: float = 0.0

    def consume_step(self, tokens: int = 0, seconds: float = 0.0) -> None:
        self.steps += 1
        self.tokens += max(0, int(tokens))
        self.elapsed += max(0.0, seconds)

    def exceeded(self) -> str | None:
        if self.steps >= self.max_steps:
            return "max_steps"
        if self.tokens >= self.max_tokens:
            return "max_tokens"
        if self.elapsed >= self.max_seconds:
            return "max_seconds"
        return None
