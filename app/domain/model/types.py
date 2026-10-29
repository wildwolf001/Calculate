"""Typed semantic model objects.

These are deliberately small and serializable. The existing SemanticLayer is
adapted into this model; the compiler keeps using the legacy raw fields until
the migration is complete.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.model.expr import MetricExpr


@dataclass(frozen=True)
class TableModel:
    id: str
    columns: tuple[str, ...] = ()
    grain: tuple[str, ...] = ()
    time: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "columns": list(self.columns),
            "grain": list(self.grain),
            "time": self.time,
        }


@dataclass(frozen=True)
class EntityModel:
    id: str
    table: str
    key: str | None = None
    grain: tuple[str, ...] = ()
    name: str | None = None
    confidence: float = 0.5
    provenance: str = "inferred"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "table": self.table,
            "key": self.key,
            "grain": list(self.grain),
            "name": self.name,
            "confidence": self.confidence,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class RelationModel:
    child: str
    parent: str
    left: str
    right: str
    cardinality: str = "N:1"
    confidence: float = 0.8
    provenance: str = "inferred"

    def as_dict(self) -> dict:
        return {
            "child": self.child,
            "parent": self.parent,
            "left": self.left,
            "right": self.right,
            "cardinality": self.cardinality,
            "confidence": self.confidence,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class MeasureModel:
    id: str
    table: str
    column: str
    aggregation: str
    additive: bool = True

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "table": self.table,
            "column": self.column,
            "aggregation": self.aggregation,
            "additive": self.additive,
        }


@dataclass(frozen=True)
class DimensionModel:
    id: str
    name: str
    kind: str
    column: str | None = None
    expr: str | None = None
    requires: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    synonyms: tuple[str, ...] = ()
    confidence: float = 0.8
    provenance: str = "inferred"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "column": self.column,
            "expr": self.expr,
            "requires": self.requires,
            "labels": dict(self.labels),
            "synonyms": list(self.synonyms),
            "confidence": self.confidence,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class TimeAxisModel:
    ref: str
    label: str | None = None
    name: str | None = None
    coverage: tuple[str, str] | None = None

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "label": self.label,
            "name": self.name,
            "coverage": list(self.coverage) if self.coverage else None,
        }


@dataclass(frozen=True)
class MetricModel:
    id: str
    name: str
    fact: str
    expr: MetricExpr
    grain: tuple[str, ...] = ()
    scope: dict[str, str] = field(default_factory=dict)
    time_axis: str | None = None
    synonyms: tuple[str, ...] = ()
    confidence: float = 0.8
    provenance: str = "inferred"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "fact": self.fact,
            "expr": self.expr.as_dict(),
            "grain": list(self.grain),
            "scope": dict(self.scope),
            "time_axis": self.time_axis,
            "synonyms": list(self.synonyms),
            "confidence": self.confidence,
            "provenance": self.provenance,
        }
