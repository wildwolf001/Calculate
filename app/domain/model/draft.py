"""Semantic draft: a model plus the questions that should be confirmed.

Inference is never treated as ground truth. A draft exposes low-confidence
objects so the UI can ask at most a couple of targeted questions before publish.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.model.model import SemanticModel
from app.domain.model.validate import validate_semantics


@dataclass(frozen=True)
class DraftIssue:
    object_id: str
    kind: str
    confidence: float
    message: str

    def as_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "confidence": self.confidence,
            "message": self.message,
        }


@dataclass(frozen=True)
class SemanticDraft:
    model: SemanticModel
    issues: tuple[DraftIssue, ...] = ()
    threshold: float = 0.6

    @classmethod
    def from_model(cls, model: SemanticModel, threshold: float = 0.6) -> "SemanticDraft":
        issues: list[DraftIssue] = []
        for entity in model.entities.values():
            if entity.key is None:
                issues.append(DraftIssue(
                    entity.id, "entity", entity.confidence,
                    f"表 {entity.table} 没有识别出主键，将按明细行粒度处理",
                ))
            elif entity.confidence < threshold:
                issues.append(DraftIssue(
                    entity.id, "entity", entity.confidence,
                    f"表 {entity.table} 的主键猜测为 {entity.key}，请确认",
                ))
        for relation in model.relations:
            if relation.confidence < threshold:
                issues.append(DraftIssue(
                    f"{relation.child}->{relation.parent}",
                    "relation",
                    relation.confidence,
                    f"表 {relation.child} 与 {relation.parent} 的关联需要确认",
                ))
        for dimension in model.dimensions.values():
            if dimension.confidence < threshold:
                issues.append(DraftIssue(
                    dimension.id, "dimension", dimension.confidence,
                    f"维度「{dimension.name}」是推断出来的，请确认",
                ))
        for metric in model.metrics.values():
            if metric.confidence < threshold:
                issues.append(DraftIssue(
                    metric.id, "metric", metric.confidence,
                    f"指标「{metric.name}」是推断出来的，请确认",
                ))
        for error in model.validate():
            issues.append(DraftIssue("model", "validation", 0.0, error))
        for issue in validate_semantics(model):
            issues.append(DraftIssue(
                issue.object_id, "semantic", 0.0,
                f"{issue.code}: {issue.message}",
            ))
        return cls(model=model, issues=tuple(issues), threshold=threshold)

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.issues)

    def as_dict(self) -> dict:
        return {
            "model_version": self.model.version,
            "requires_confirmation": self.requires_confirmation,
            "issues": [issue.as_dict() for issue in self.issues],
        }
