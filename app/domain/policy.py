"""Policy decisions kept separate from capability.

Capability answers "can this dataset compute the combination"; policy answers
"is this caller allowed to see it". The default is allow-all so the migration
does not change behavior.
"""
from __future__ import annotations

from dataclasses import dataclass

MISSING_METRIC = "MISSING_METRIC"
NOT_REACHABLE = "NOT_REACHABLE"
FANOUT_UNSAFE = "FANOUT_UNSAFE"
OUT_OF_COVERAGE = "OUT_OF_COVERAGE"
ACCESS_DENIED = "ACCESS_DENIED"
AMBIGUOUS = "AMBIGUOUS"
LOW_CONFIDENCE = "LOW_CONFIDENCE"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str | None = None
    reason: str | None = None


class PolicySet:
    """Default policy: everything is allowed."""

    def check(self, metric: str, dimensions: tuple[str, ...] = (),
              user: object | None = None) -> PolicyDecision:
        return PolicyDecision(True)


class DenyAllPolicy(PolicySet):
    """Test/placeholder policy."""

    def check(self, metric: str, dimensions: tuple[str, ...] = (),
              user: object | None = None) -> PolicyDecision:
        return PolicyDecision(False, ACCESS_DENIED, "当前策略不允许访问该指标")
