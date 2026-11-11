"""Deterministic semantic candidate resolution.

The LLM may propose a metric, but it must not be the final authority. This
module maps question text to published metrics using exact names, aliases,
synonyms and Chinese partial matches, and returns unique / ambiguous / missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.semantic.loader import SemanticLayer


def _common_len(term: str, text: str) -> int:
    best = 0
    for start in range(len(term)):
        for end in range(start + 2, len(term) + 1):
            part = term[start:end]
            if part in text:
                best = max(best, len(part))
    return best


AVG_MARKERS = ("平均", "均值", "评分", "平均分", "客单价", "均价")
RATIO_MARKERS = ("率", "占比", "比例", "百分比")
SUM_MARKERS = (
    "合计", "总额", "总和", "总金额", "销售额", "销售金额",
    "金额", "营收", "收入", "支出", "花费", "费用", "成本",
)
COUNT_MARKERS = (
    "多少", "数量", "笔数", "个数", "单数", "次数",
    "人数", "家数", "件数", "条数",
)


def requested_measure(text: str) -> str | None:
    """Infer the aggregation the question asks for, if it is explicit.

    ``多少`` is deliberately checked last: "平均评分是多少" asks for an
    average, not a count, even though it contains a generic question word.
    """
    value = str(text or "")
    if any(marker in value for marker in AVG_MARKERS):
        return "avg"
    if any(marker in value for marker in RATIO_MARKERS):
        return "ratio"
    if any(marker in value for marker in SUM_MARKERS):
        return "sum"
    if any(marker in value for marker in COUNT_MARKERS):
        return "count"
    return None


def measure_kind(metric) -> str:
    """Classify a metric as avg/sum/count/ratio/other from its expression."""
    raw = getattr(metric, "raw", {}) or {}
    metric_type = str(getattr(metric, "type", "") or raw.get("type") or "")
    if metric_type == "ratio":
        return "ratio"
    if metric_type == "entity_count":
        return "count"
    aggregation = str(raw.get("aggregation") or "").upper()
    if aggregation in {"AVG", "SUM", "COUNT"}:
        return aggregation.lower()
    expr = str(raw.get("expr") or "").upper()
    if "AVG(" in expr:
        return "avg"
    if "SUM(" in expr:
        return "sum"
    if "COUNT(" in expr:
        return "count"
    if "/" in expr and "NULLIF" in expr:
        return "ratio"
    return "other"


def metric_text_support(metric, text: str) -> bool:
    """True when a full metric name or synonym literally appears in the text."""
    if metric is None:
        return False
    terms = [getattr(metric, "name", ""), *getattr(metric, "synonyms", [])]
    return any(term and str(term) in str(text or "") for term in terms)


@dataclass(frozen=True)
class MetricCandidate:
    metric_id: str
    name: str
    score: int
    matched: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "score": self.score,
            "matched": list(self.matched),
        }


@dataclass(frozen=True)
class Resolution:
    status: str                       # unique | ambiguous | missing
    candidates: tuple[MetricCandidate, ...] = ()
    selected: str | None = None
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "selected": self.selected,
            "reason": self.reason,
            "candidates": [c.as_dict() for c in self.candidates],
        }


class SemanticResolver:
    def __init__(self, sem: SemanticLayer):
        self.sem = sem

    def _score(self, metric, text: str) -> tuple[int, tuple[str, ...]]:
        score = 0
        matched: list[str] = []
        name = str(metric.name or "")
        if name:
            if name in text:
                score += 100
                matched.append(name)
            else:
                common = _common_len(name, text)
                if common >= 2:
                    score += min(common, 8) * 10
                    matched.append(name)
        for synonym in metric.synonyms:
            if not synonym:
                continue
            if synonym in text:
                if 80 > score:
                    score = 80
                matched.append(synonym)
            else:
                common = _common_len(str(synonym), text)
                if common >= 2:
                    # Best synonym, not sum: summing aliases made generic
                    # words like "订单" dominate every order-related metric.
                    score = max(score, min(common, 8) * 8)
                    matched.append(str(synonym))
        # A question asking for a "率/占比/比例" should prefer a published
        # ratio metric over a generic amount/count metric that merely shares
        # words like "订单" or "已送达".
        if metric.type == "ratio" and any(
            marker in text for marker in ("率", "占比", "比例")
        ) and any(
            marker in f"{name} {' '.join(str(s) for s in metric.synonyms)}"
            for marker in ("率", "占比", "比例")
        ):
            score += 30
        return score, tuple(dict.fromkeys(matched))

    def resolve(self, text: str) -> Resolution:
        value = str(text or "")
        scored: list[MetricCandidate] = []
        for metric in self.sem.metrics.values():
            score, matched = self._score(metric, value)
            if score > 0:
                scored.append(MetricCandidate(metric.id, metric.name, score, matched))
        scored.sort(key=lambda item: (-item.score, item.metric_id))
        if not scored:
            return Resolution("missing", reason="没有匹配到已发布指标")
        top = scored[0]
        second = scored[1] if len(scored) > 1 else None
        if len(scored) == 1 or top.score - (second.score if second else 0) >= 10:
            return Resolution("unique", tuple(scored[:5]), top.metric_id,
                              f"命中指标「{top.name}」")
        return Resolution("ambiguous", tuple(scored[:5]), None,
                          "多个指标都可能匹配，需要澄清")
