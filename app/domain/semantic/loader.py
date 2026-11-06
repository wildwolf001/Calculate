"""Load and validate the semantic layer + physical binding.

Failure policy: a missing table/column or an unresolved reference raises at
startup. The agent never silently degrades to "best effort" business logic.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml

from app.config.settings import BINDING_PATH, CATALOG_PATH, SEMANTIC_PATH, WAREHOUSE_PATH


class SemanticError(RuntimeError):
    pass


@dataclass
class Metric:
    id: str
    raw: dict[str, Any]

    @property
    def name(self) -> str:
        return self.raw.get("name_zh", self.id)

    @property
    def type(self) -> str:
        return self.raw.get("type", "aggregate")

    @property
    def fact(self) -> str:
        return self.raw["fact"]

    @property
    def supported_grain(self) -> set[str]:
        return set(self.raw.get("supported_grain", ["none"]))

    @property
    def synonyms(self) -> list[str]:
        return self.raw.get("synonyms", [])


@dataclass
class Dimension:
    id: str
    raw: dict[str, Any]

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "categorical")

    @property
    def join_path(self) -> list[str]:
        return self.raw.get("join_path", [])


@dataclass
class Entity:
    id: str
    raw: dict[str, Any]

    @property
    def table(self) -> str:
        return str(self.raw.get("table") or "")

    @property
    def key(self) -> str:
        return str(self.raw.get("key") or "")

    @property
    def label(self) -> str:
        return str(self.raw.get("label") or self.raw.get("name_zh") or self.id)

    @property
    def aliases(self) -> list[str]:
        return [str(item) for item in (self.raw.get("aliases") or []) if item]


@dataclass
class SemanticLayer:
    version: int
    dataset: str
    tables: dict[str, Any]
    joins: dict[str, Any]
    pre_aggregates: dict[str, Any]
    metrics: dict[str, Metric]
    dimensions: dict[str, Dimension]
    policies: dict[str, Any]
    time_range: tuple[str, str]
    binding: dict[str, Any]
    capabilities: dict[str, Any] = field(default_factory=dict)
    parser: str = "generic"
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    time_axes: list[dict[str, Any]] = field(default_factory=list)
    entities: dict[str, Entity] = field(default_factory=dict)

    # ---- lookup helpers ----
    def metric(self, mid: str) -> Metric:
        if mid not in self.metrics:
            raise SemanticError(f"未知指标: {mid}")
        return self.metrics[mid]

    def dimension(self, did: str) -> Dimension:
        if did not in self.dimensions:
            raise SemanticError(f"未知维度: {did}")
        return self.dimensions[did]

    def find_metric_by_synonym(self, text: str) -> list[Metric]:
        hits = []
        for m in self.metrics.values():
            terms = [m.name, *m.synonyms]
            if any(t and t in text for t in terms):
                hits.append(m)
        return hits

    def column(self, ref: str) -> str:
        """Resolve `table.logical_col` to a physical column name."""
        table, _, col = ref.partition(".")
        if not col:
            return self.binding.get("tables", {}).get(table, {}).get(ref, ref)
        return self.binding.get("tables", {}).get(table, {}).get(col, col)


def normalize_date_bound(value: Any) -> str:
    """Semantic ranges are date-only; tolerate timestamps from inferred minima."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return text


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise SemanticError(f"找不到文件: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SemanticError(f"{path} 结构非法")
    return data


def _catalog_tables(catalog_path: Path = CATALOG_PATH) -> dict[str, set[str]]:
    if not catalog_path.exists():
        raise SemanticError(f"catalog.json 不存在，请先运行 ingest: {catalog_path}")
    cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    return {
        name: {c["name"] for c in tbl["columns"]}
        for name, tbl in cat["tables"].items()
    }


def validate(sem: dict, binding: dict, catalog_path: Path = CATALOG_PATH) -> None:
    errors: list[str] = []
    physical = _catalog_tables(catalog_path)

    # 1) binding covers everything semantic references
    for table, cols in binding.get("tables", {}).items():
        if table not in physical:
            errors.append(f"binding: 物理表不存在 {table}")
            continue
        for logical, phys in cols.items():
            if phys not in physical[table]:
                errors.append(f"binding: {table}.{logical} -> {phys} 不存在")

    # 2) every semantic table exists and columns resolve
    for table, spec in sem.get("tables", {}).items():
        if table not in physical:
            errors.append(f"semantic: 表不存在 {table}")
            continue
        for col in spec.get("columns", []):
            phys = binding.get("tables", {}).get(table, {}).get(col, col)
            if phys not in physical[table]:
                errors.append(f"semantic: {table}.{col} 无法绑定")

    # 3) metrics
    dims = set(sem.get("dimensions", {}))
    for mid, m in sem.get("metrics", {}).items():
        if m.get("fact") not in physical:
            errors.append(f"metric {mid}: fact 表不存在 {m.get('fact')}")
        for g in m.get("supported_grain", []):
            if g != "none" and g not in dims:
                errors.append(f"metric {mid}: supported_grain 含未知维度 {g}")
        if m.get("type") == "ratio" and not m.get("denominator"):
            errors.append(f"metric {mid}: ratio 缺少 denominator")
        if m.get("type") == "entity_count" and not m.get("entity"):
            errors.append(f"metric {mid}: entity_count 缺少 entity")

    # 4) join columns must resolve and be N:1/1:1
    joins = sem.get("joins", {})
    for child, links in joins.items():
        if child not in physical:
            errors.append(f"join: 子表不存在 {child}")
            continue
        for parent, spec in links.items():
            if parent not in physical:
                errors.append(f"join {child}->{parent}: 父表不存在")
                continue
            left = spec.get("left") or spec.get("on")
            right = spec.get("right") or spec.get("on")
            if not left or not right:
                errors.append(f"join {child}->{parent}: 缺少连接列")
                continue
            if left not in physical[child]:
                errors.append(f"join {child}->{parent}: 子表列不存在 {left}")
            if right not in physical[parent]:
                errors.append(f"join {child}->{parent}: 父表列不存在 {right}")
            if spec.get("card") not in {"N:1", "1:1"}:
                errors.append(f"join {child}->{parent}: 不是 N:1/1:1")

    # 5) dimension join paths must be declared N:1
    for did, d in sem.get("dimensions", {}).items():
        for step in d.get("join_path", []):
            src, _, dst = step.partition(" -> ")
            if dst not in joins.get(src, {}):
                errors.append(f"dimension {did}: 未声明 join {step}")
            elif joins[src][dst].get("card") not in {"N:1", "1:1"}:
                errors.append(f"dimension {did}: join {step} 不是 N:1")

    if errors:
        raise SemanticError("语义层校验失败:\n  - " + "\n  - ".join(errors))



def _split_ref(ref: str) -> tuple[str, str]:
    table, _, column = str(ref or "").partition(".")
    return table, column


def _entity_supported_grain(sem: SemanticLayer, entity_table: str,
                            fact_table: str) -> list[str]:
    supported = ["none"]
    for dim in sem.dimensions.values():
        if dim.kind == "time":
            supported.append(dim.id)
            continue
        column = dim.raw.get("column")
        table = _split_ref(column)[0] if column else ""
        requires = str(dim.raw.get("requires") or "")
        if table in {entity_table, fact_table} or requires in {entity_table, fact_table}:
            supported.append(dim.id)
    return list(dict.fromkeys(supported))


def _expand_entities(sem: SemanticLayer) -> None:
    """Fill the entity registry and generate neutral entity-count metrics.

    Entity-count metrics are pre-coordinated macros (entity + scope + threshold).
    This turns their entity/event metadata into a reusable registry and adds a
    min_events=1 metric per entity, so "how many customers" no longer depends
    on a metric whose name happens to contain 复购.
    """
    explicit = sem.raw.get("entities") or {}
    if isinstance(explicit, dict):
        for entity_id, raw in explicit.items():
            if isinstance(raw, dict):
                sem.entities[str(entity_id)] = Entity(str(entity_id), dict(raw))

    def find_entity(table: str, key: str):
        for entity in sem.entities.values():
            if entity.table == table and entity.key == key:
                return entity
        return None

    for metric in list(sem.metrics.values()):
        if metric.type != "entity_count":
            continue
        table, key = _split_ref(metric.raw.get("entity") or "")
        if not table or not key:
            continue
        entity = find_entity(table, key)
        if entity is None:
            name = metric.name
            label = str(metric.raw.get("entity_label") or name)
            for prefix in ("复购", "重复", "多次"):
                label = label.replace(prefix, "")
            if label.endswith("数"):
                label = label[:-1]
            label = label or table
            entity_id = str(metric.raw.get("entity_id") or table)
            aliases = [label, f"自然{label}", f"去重{label}"]
            event_key = ""
            match = re.search(r"(t_[A-Za-z0-9_]+\.[A-Za-z0-9_]+)",
                              str(metric.raw.get("count_orders") or ""))
            if match:
                event_key = match.group(1)
            if not event_key:
                grain = (sem.tables.get(metric.fact) or {}).get("grain") or []
                if grain:
                    event_key = f"{metric.fact}.{grain[0]}"
            entity = Entity(entity_id, {
                "table": table,
                "key": key,
                "label": label,
                "aliases": aliases,
                "unit": metric.raw.get("unit"),
                "event": {
                    "fact": metric.fact,
                    "key": event_key,
                    "scope": dict(metric.raw.get("scope") or {}),
                    "time_via": metric.raw.get("time_via"),
                    "join": metric.raw.get("entity_join"),
                },
            })
            sem.entities[entity.id] = entity

    for entity in list(sem.entities.values()):
        event = entity.raw.get("event") or {}
        fact = str(event.get("fact") or "")
        if not fact or fact not in sem.tables:
            continue
        metric_id = f"entity_count_{entity.id}"
        if metric_id in sem.metrics:
            continue
        label = entity.label
        exact_name = f"{label}数"
        if (
            exact_name in {metric.name for metric in sem.metrics.values()}
            or "unique" in entity.key.lower()
        ) and not label.startswith("自然"):
            exact_name = f"自然{label}数"
        if any(
            metric.type == "entity_count"
            and metric.raw.get("entity") == f"{entity.table}.{entity.key}"
            and metric.name == exact_name
            for metric in sem.metrics.values()
        ):
            continue
        event_key = str(event.get("key") or "")
        if not event_key:
            grain = (sem.tables.get(fact) or {}).get("grain") or []
            if grain:
                event_key = f"{fact}.{grain[0]}"
        count_orders = (
            f"COUNT(DISTINCT {event_key})" if event_key else "COUNT(*)"
        )
        # Do not add the bare label: it would hijack an existing
        # order-scoped metric such as 客户数.  Natural/deduplicated aliases are
        # explicit enough for the entity-count binder.
        aliases = [
            alias for alias in dict.fromkeys([
                f"自然{label}", f"去重{label}", exact_name,
                *entity.aliases,
            ])
            if alias != label
        ]
        sem.metrics[metric_id] = Metric(metric_id, {
            "name_zh": exact_name,
            "type": "entity_count",
            "fact": fact,
            "entity": f"{entity.table}.{entity.key}",
            "entity_join": event.get("join") or f"{fact} -> {entity.table}",
            "count_orders": count_orders,
            "min_orders": 1,
            "scope": dict(event.get("scope") or {}),
            "time_via": event.get("time_via"),
            "supported_grain": _entity_supported_grain(
                sem, entity.table, fact
            ),
            "unit": entity.raw.get("unit") or "个",
            "definition": f"按 {entity.key} 去重的{label}数",
            "synonyms": aliases,
            "provenance": "derived",
            "confidence": 0.7,
        })

def expand_entities(sem: SemanticLayer) -> None:
    """Public hook for tests/callers that construct SemanticLayer directly."""
    _expand_entities(sem)


def materialize_semantic(
    semantic: dict[str, Any],
    metrics: dict[str, Any],
    dimensions: dict[str, Any],
    keep_dims: list[str] | None = None,
) -> dict[str, Any]:
    """Build-time entity registry and generated entity metrics.

    Publishing calls this once and writes the result into semantic.yaml.  The
    runtime loader deliberately does not invent entities or metric names.
    """
    data = dict(semantic)
    data["metrics"] = {mid: dict(raw) for mid, raw in metrics.items()}
    data["dimensions"] = {did: dict(raw) for did, raw in dimensions.items()}
    dim_map = {
        did: Dimension(did, raw) for did, raw in data["dimensions"].items()
    }
    layer = SemanticLayer(
        version=int(data.get("version", 1)),
        dataset=str(data.get("dataset") or ""),
        tables=data.get("tables") or {},
        joins=data.get("joins") or {},
        pre_aggregates=data.get("pre_aggregates") or {},
        metrics={mid: Metric(mid, raw) for mid, raw in data["metrics"].items()},
        dimensions=dim_map,
        policies=data.get("policies") or {},
        time_range=tuple(
            normalize_date_bound(v)
            for v in (data.get("time_range") or ["", ""])
        ),
        binding={"tables": {}},
        capabilities=data.get("capabilities") or {},
        parser=str(data.get("parser") or "generic"),
        raw=data,
        time_axes=list(data.get("time_axes") or []),
    )
    _expand_entities(layer)
    data["entities"] = {
        entity_id: json.loads(json.dumps(entity.raw, ensure_ascii=False))
        for entity_id, entity in layer.entities.items()
    }
    allowed = set(keep_dims or [])
    for mid, metric in layer.metrics.items():
        if mid in data["metrics"]:
            continue
        raw = dict(metric.raw)
        if allowed:
            raw["supported_grain"] = [
                grain for grain in (raw.get("supported_grain") or ["none"])
                if grain in {"none", *allowed}
            ]
        data["metrics"][mid] = raw
    return data


def load_semantic(
    semantic_path: Path = SEMANTIC_PATH,
    binding_path: Path = BINDING_PATH,
    catalog_path: Path = CATALOG_PATH,
) -> SemanticLayer:
    sem = _load_yaml(semantic_path)
    binding = _load_yaml(binding_path)
    validate(sem, binding, catalog_path=catalog_path)
    metrics = {mid: Metric(mid, m) for mid, m in sem.get("metrics", {}).items()}
    dimensions = {did: Dimension(did, d) for did, d in sem.get("dimensions", {}).items()}
    layer = SemanticLayer(
        version=int(sem.get("version", 1)),
        dataset=sem.get("dataset", ""),
        tables=sem.get("tables", {}),
        joins=sem.get("joins", {}),
        pre_aggregates=sem.get("pre_aggregates", {}),
        metrics=metrics,
        dimensions=dimensions,
        policies=sem.get("policies", {}),
        time_range=tuple(
            normalize_date_bound(v) for v in sem.get("time_range", ["", ""])
        ),
        binding=binding,
        capabilities=sem.get("capabilities", {}),
        parser=str(sem.get("parser", "generic")),
        raw=sem,
        time_axes=list(sem.get("time_axes") or []),
        entities={
            str(entity_id): Entity(str(entity_id), dict(raw))
            for entity_id, raw in (sem.get("entities") or {}).items()
            if isinstance(raw, dict)
        },
    )
    return layer


def available_dimension_values(dim_id: str,
                               db_path: Path = WAREHOUSE_PATH,
                               sem: SemanticLayer | None = None) -> list:
    """Read distinct values for grounding. Never trust hard-coded lists."""
    sem = sem or load_semantic()
    dim = sem.dimension(dim_id)
    col = dim.raw.get("column") or dim.raw.get("filter_column")
    if not col:
        return []
    table, _, colname = col.partition(".")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            f'SELECT DISTINCT "{colname}" FROM "{table}" WHERE "{colname}" IS NOT NULL ORDER BY 1'
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()
