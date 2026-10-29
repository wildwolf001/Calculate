"""Typed semantic model and adapter from the legacy SemanticLayer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.model.expr import (
    AggExpr,
    CountDistinctExpr,
    MetricExpr,
    from_metric_raw,
)
from app.domain.model.types import (
    DimensionModel,
    EntityModel,
    MeasureModel,
    MetricModel,
    RelationModel,
    TableModel,
    TimeAxisModel,
)


def _columns(raw: dict[str, Any]) -> tuple[str, ...]:
    columns = raw.get("columns") or []
    names: list[str] = []
    for column in columns:
        if isinstance(column, dict):
            name = column.get("name") or column.get("id")
        else:
            name = column
        if name:
            names.append(str(name))
    return tuple(names)


def _grain(raw: dict[str, Any]) -> tuple[str, ...]:
    grain = raw.get("grain") or raw.get("key") or []
    if isinstance(grain, str):
        grain = [grain]
    return tuple(str(item) for item in grain)


@dataclass(frozen=True)
class SemanticModel:
    dataset: str
    version: str
    tables: dict[str, TableModel]
    entities: dict[str, EntityModel]
    relations: tuple[RelationModel, ...]
    measures: dict[str, MeasureModel]
    dimensions: dict[str, DimensionModel]
    metrics: dict[str, MetricModel]
    time_axes: dict[str, TimeAxisModel]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_layer(cls, layer: Any) -> "SemanticModel":
        tables: dict[str, TableModel] = {}
        for table_id, raw in (layer.tables or {}).items():
            tables[table_id] = TableModel(
                id=str(table_id),
                columns=_columns(raw),
                grain=_grain(raw),
                time=(str(raw["time"]) if raw.get("time") else None),
            )

        entities: dict[str, EntityModel] = {}
        for table_id, table in tables.items():
            key = table.grain[0] if table.grain else None
            entities[f"e_{table_id}"] = EntityModel(
                id=f"e_{table_id}",
                table=table_id,
                key=key,
                grain=table.grain,
                confidence=0.7 if key else 0.2,
            )

        relations: list[RelationModel] = []
        for child, parents in (layer.joins or {}).items():
            for parent, spec in (parents or {}).items():
                left = str(spec.get("left") or spec.get("on") or "")
                right = str(spec.get("right") or spec.get("on") or "")
                relations.append(RelationModel(
                    child=str(child),
                    parent=str(parent),
                    left=left,
                    right=right,
                    cardinality=str(spec.get("card") or "N:1"),
                    confidence=float(spec.get("confidence", 0.8)),
                    provenance=str(spec.get("provenance") or "inferred"),
                ))

        dimensions: dict[str, DimensionModel] = {}
        for dim_id, dim in (layer.dimensions or {}).items():
            raw = dim.raw or {}
            dimensions[str(dim_id)] = DimensionModel(
                id=str(dim_id),
                name=str(dim.raw.get("name_zh") or dim_id),
                kind=str(dim.kind),
                column=(str(raw["column"]) if raw.get("column") else None),
                expr=(str(raw["expr"]) if raw.get("expr") else None),
                requires=(str(raw["requires"]) if raw.get("requires") else None),
                labels={str(k): str(v) for k, v in (raw.get("labels") or {}).items()},
                synonyms=tuple(str(s) for s in (raw.get("synonyms") or [])),
                confidence=float(raw.get("confidence", 0.8)),
                provenance=str(raw.get("provenance") or "inferred"),
            )

        measures: dict[str, MeasureModel] = {}
        metrics: dict[str, MetricModel] = {}
        for metric_id, metric in (layer.metrics or {}).items():
            raw = metric.raw or {}
            fact = str(raw.get("fact") or "")
            table = tables.get(fact)
            expr = from_metric_raw(raw)
            metrics[str(metric_id)] = MetricModel(
                id=str(metric_id),
                name=str(metric.name),
                fact=fact,
                expr=expr,
                grain=(
                    tuple(str(item) for item in raw.get("grain") or [])
                    or (table.grain if table else ())
                ),
                scope={str(k): str(v) for k, v in (raw.get("scope") or {}).items()},
                time_axis=(str(raw["time_via"]) if raw.get("time_via") else None),
                synonyms=tuple(str(s) for s in (metric.synonyms or [])),
                confidence=float(raw.get("confidence", 0.8)),
                provenance=str(raw.get("provenance") or "inferred"),
            )
            measure = _measure_from_expr(str(metric_id), fact, expr)
            if measure is not None:
                measures[measure.id] = measure

        time_axes: dict[str, TimeAxisModel] = {}
        for axis in (getattr(layer, "time_axes", None) or []):
            ref = str(axis.get("ref") or "")
            if not ref:
                continue
            time_axes[ref] = TimeAxisModel(
                ref=ref,
                label=(str(axis["label"]) if axis.get("label") else None),
                name=(str(axis["name_zh"]) if axis.get("name_zh") else None),
                coverage=tuple(layer.time_range) if layer.time_range else None,
            )
        if not time_axes:
            for table_id, table in tables.items():
                if table.time:
                    ref = f"{table_id}.{table.time}"
                    time_axes[ref] = TimeAxisModel(
                        ref=ref,
                        label=table.time,
                        name=table.time,
                        coverage=tuple(layer.time_range) if layer.time_range else None,
                    )

        model = cls(
            dataset=str(getattr(layer, "dataset", "") or ""),
            version=str(getattr(layer, "version", "") or ""),
            tables=tables,
            entities=entities,
            relations=tuple(relations),
            measures=measures,
            dimensions=dimensions,
            metrics=metrics,
            time_axes=time_axes,
            raw={"source": "SemanticLayer"},
        )
        model.validate()
        return model

    def validate(self) -> list[str]:
        errors: list[str] = []
        for relation in self.relations:
            if relation.child not in self.tables:
                errors.append(f"relation child missing: {relation.child}")
            if relation.parent not in self.tables:
                errors.append(f"relation parent missing: {relation.parent}")
        for metric in self.metrics.values():
            if metric.fact and metric.fact not in self.tables:
                errors.append(f"metric fact missing: {metric.id} -> {metric.fact}")
            if metric.time_axis:
                table = metric.time_axis.split(".", 1)[0]
                if table not in self.tables:
                    errors.append(f"metric time axis missing: {metric.id}")
        for dimension in self.dimensions.values():
            if dimension.requires and dimension.requires not in self.tables:
                errors.append(f"dimension requires missing: {dimension.id}")
            if dimension.column:
                table = dimension.column.split(".", 1)[0]
                if table not in self.tables:
                    errors.append(f"dimension column missing: {dimension.id}")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "tables": {k: v.as_dict() for k, v in self.tables.items()},
            "entities": {k: v.as_dict() for k, v in self.entities.items()},
            "relations": [r.as_dict() for r in self.relations],
            "measures": {k: v.as_dict() for k, v in self.measures.items()},
            "dimensions": {k: v.as_dict() for k, v in self.dimensions.items()},
            "metrics": {k: v.as_dict() for k, v in self.metrics.items()},
            "time_axes": {k: v.as_dict() for k, v in self.time_axes.items()},
            "errors": self.validate(),
        }


def _measure_from_expr(metric_id: str, fact: str, expr: MetricExpr) -> MeasureModel | None:
    if isinstance(expr, AggExpr):
        column = expr.operand.rsplit(".", 1)[-1]
        return MeasureModel(
            id=metric_id,
            table=fact,
            column=column,
            aggregation=expr.aggregation.lower(),
            additive=expr.aggregation.upper() == "SUM",
        )
    if isinstance(expr, CountDistinctExpr):
        column = expr.operand.rsplit(".", 1)[-1]
        return MeasureModel(
            id=metric_id,
            table=fact,
            column=column,
            aggregation="count_distinct",
            additive=False,
        )
    return None
