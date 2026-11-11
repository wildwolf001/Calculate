"""Published model card: the single structured description of a dataset.

The card is a **generated read model**: it combines the semantic layer, the
physical catalog and the profile into one object that the brief, the type
guard, the publish checks and the admin UI all consume. It is never hand
edited; optional human edits live in ``model_overrides.json`` and are merged
on top.

The card is intentionally independent of any UI/LLM format: the brief is one
projection of it, not the card itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.domain.semantic.loader import SemanticLayer

NUMERIC_TYPES = {
    "integer", "int", "bigint", "smallint", "tinyint", "hugeint",
    "decimal", "numeric", "float", "double", "real",
}
AGG_RE = re.compile(
    r"^(AVG|SUM|COUNT)\(\s*(?:DISTINCT\s+)?([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def is_numeric_type(value: Any) -> bool:
    return str(value or "").strip().lower() in NUMERIC_TYPES


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def overrides_path(root: Path) -> Path:
    return root / "model_overrides.json"


def load_overrides(root: Path) -> dict[str, Any]:
    return _read_json(overrides_path(root))


def _profile_columns(profile: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for table in (profile or {}).get("tables") or []:
        table_id = str(table.get("table_id") or "")
        if not table_id:
            continue
        tables[table_id] = {
            str(col.get("physical")): col for col in (table.get("columns") or [])
        }
    return tables


def _catalog_columns(catalog: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    tables: dict[str, dict[str, dict[str, Any]]] = {}
    for table_id, table in ((catalog or {}).get("tables") or {}).items():
        tables[table_id] = {
            str(col.get("name")): col for col in (table.get("columns") or [])
        }
    return tables


def _column_role(
    column: str,
    *,
    table_id: str,
    column_type: str,
    grain: list[str],
    forks: dict[str, str],
    time_columns: set[str],
    explicit: str | None = None,
) -> str:
    if explicit:
        return explicit
    if column in grain:
        return "pk"
    if column in forks:
        return "fk"
    if column in time_columns or str(column_type or "").lower() in {"date", "datetime"}:
        return "time"
    if is_numeric_type(column_type):
        return "measure"
    return "dimension"


def _metric_spec(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (aggregation, operand) for simple aggregate expressions."""
    expr = str(raw.get("expr") or "").strip()
    if not expr:
        return None, None
    match = AGG_RE.match(expr)
    if not match:
        return None, None
    aggregation = match.group(1).lower()
    operand = match.group(2)
    if aggregation == "count" and "distinct" in expr[:24].lower():
        aggregation = "count_distinct"
    return aggregation, operand


def build_model_card(
    *,
    dataset_id: str,
    sem: SemanticLayer,
    catalog: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    documentation: dict[str, Any] | None = None,
    data_version: str = "",
) -> dict[str, Any]:
    catalog = catalog or {}
    profile = profile or {}
    overrides = overrides or {}
    catalog_cols = _catalog_columns(catalog)
    profile_cols = _profile_columns(profile)
    table_overrides = (overrides.get("tables") or {})

    # child table -> {child_col, parent, parent_col}
    forks: dict[str, dict[str, dict[str, str]]] = {}
    for child, parents in (sem.joins or {}).items():
        for parent, rel in (parents or {}).items():
            left = str(rel.get("left") or rel.get("on") or "")
            right = str(rel.get("right") or rel.get("on") or "")
            if left:
                forks.setdefault(child, {})[left] = {
                    "parent": parent, "parent_col": right,
                }

    metric_facts = {
        str((metric.raw or {}).get("fact") or "") for metric in sem.metrics.values()
    }
    parent_tables = {
        parent for parents in (sem.joins or {}).values() for parent in parents
    }

    tables: dict[str, Any] = {}
    for table_id, raw in (sem.tables or {}).items():
        table_meta = (catalog.get("tables") or {}).get(table_id) or {}
        profile_table = (
            next(
                (item for item in (profile.get("tables") or [])
                 if str(item.get("table_id")) == table_id),
                {},
            )
        )
        override = table_overrides.get(table_id) or {}
        grain = [str(item) for item in (raw.get("grain") or [])]
        time = str(raw.get("time") or "")
        columns = []
        physical_names = list(raw.get("columns") or [])
        for name in physical_names:
            name = str(name)
            cat_col = (catalog_cols.get(table_id) or {}).get(name) or {}
            prof_col = (profile_cols.get(table_id) or {}).get(name) or {}
            col_override = (override.get("columns") or {}).get(name) or {}
            column_type = str(cat_col.get("type") or prof_col.get("type") or "")
            role = _column_role(
                name,
                table_id=table_id,
                column_type=column_type,
                grain=grain,
                forks=forks.get(table_id) or {},
                time_columns={time} if time else set(),
                explicit=str(col_override.get("role") or cat_col.get("role") or "") or None,
            )
            fk = (forks.get(table_id) or {}).get(name)
            top_values = [
                item.get("value") if isinstance(item, dict) else item
                for item in (prof_col.get("top_values") or [])
            ]
            columns.append({
                "name": name,
                "label": str(col_override.get("label") or cat_col.get("label") or name),
                "type": column_type,
                "role": role,
                "unit": col_override.get("unit") or prof_col.get("unit") or cat_col.get("unit"),
                "pii": bool(cat_col.get("is_pii") or prof_col.get("pii")),
                "description": str(col_override.get("description") or ""),
                "min": prof_col.get("min"),
                "max": prof_col.get("max"),
                "top_values": top_values[:5],
                "distinct": cat_col.get("distinct", prof_col.get("distinct")),
                **({"fk": f"{fk['parent']}.{fk['parent_col']}"} if fk else {}),
            })
        role = (
            "fact" if table_id in metric_facts
            else ("dimension" if table_id in parent_tables else "standalone")
        )
        tables[table_id] = {
            "label": str(override.get("label") or profile_table.get("label") or table_id),
            "role": role,
            "rows": (catalog.get("tables") or {}).get(table_id, {}).get("rows"),
            "grain": grain,
            "primary_key": grain,
            "time": time or None,
            "description": str(override.get("description") or ""),
            "columns": columns,
        }

    metrics: list[dict[str, Any]] = []
    for metric_id, metric in (sem.metrics or {}).items():
        raw = metric.raw or {}
        aggregation, operand = _metric_spec(raw)
        if raw.get("type") == "ratio":
            aggregation = "ratio"
        elif raw.get("type") == "entity_count":
            aggregation = "entity_count"
        metrics.append({
            "id": metric_id,
            "name": metric.name,
            "type": metric.type,
            "aggregation": aggregation,
            "operand": operand,
            "expr": raw.get("expr"),
            "numerator": raw.get("numerator"),
            "denominator": raw.get("denominator"),
            "fact": raw.get("fact"),
            "scope": dict(raw.get("scope") or {}),
            "time_via": raw.get("time_via"),
            "unit": raw.get("unit"),
            "synonyms": list(raw.get("synonyms") or []),
            "definition": raw.get("definition"),
            "provenance": raw.get("provenance"),
            "supported_dimensions": list(raw.get("supported_grain") or []),
        })

    dimensions = []
    for dim_id, dim in (sem.dimensions or {}).items():
        raw = dim.raw or {}
        dimensions.append({
            "id": dim_id,
            "name": str(raw.get("name_zh") or dim_id),
            "kind": dim.kind,
            "column": raw.get("column"),
            "expr": raw.get("expr"),
            "requires": raw.get("requires"),
            "labels": dict(raw.get("labels") or {}),
            "synonyms": list(raw.get("synonyms") or []),
        })

    relations = []
    for child, parents in (sem.joins or {}).items():
        for parent, rel in (parents or {}).items():
            relations.append({
                "from": f"{child}.{rel.get('left') or rel.get('on') or ''}",
                "to": f"{parent}.{rel.get('right') or rel.get('on') or ''}",
                "card": rel.get("card") or "N:1",
            })

    docs = []
    for rule in (documentation or {}).get("rules") or []:
        if isinstance(rule, dict):
            docs.append({
                "name": rule.get("name"),
                "definition": rule.get("definition"),
                "formula": rule.get("formula"),
            })

    card = {
        "version": 1,
        "dataset_id": dataset_id,
        "data_version": data_version,
        "semantic_version": sem.version,
        "semantic_fingerprint": _semantic_fingerprint(sem),
        "overrides_hash": _overrides_hash(overrides),
        "tables": tables,
        "relations": sorted(relations, key=lambda item: (item["from"], item["to"])),
        "metrics": metrics,
        "dimensions": dimensions,
        "time_axes": list(sem.time_axes or []),
        "docs": docs,
    }
    return card


def _semantic_fingerprint(sem: SemanticLayer) -> str:
    from app.datasets.index import semantic_fingerprint

    return semantic_fingerprint(sem)


def _overrides_hash(overrides: dict[str, Any]) -> str:
    payload = json.dumps(overrides or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def model_card_fingerprint(sem: SemanticLayer, overrides: dict[str, Any] | None = None) -> tuple[str, str]:
    return _semantic_fingerprint(sem), _overrides_hash(overrides or {})


def validate_model_card(card: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    tables = card.get("tables") or {}
    for table_id, table in tables.items():
        if not table.get("columns"):
            issues.append(f"表 {table_id} 没有列")
        for column in table.get("columns") or []:
            role = column.get("role")
            if role not in {"pk", "fk", "measure", "time", "dimension"}:
                issues.append(f"{table_id}.{column.get('name')}: 未知角色 {role}")
    for relation in card.get("relations") or []:
        for ref in (str(relation.get("from") or ""), str(relation.get("to") or "")):
            table_id, _, column = ref.partition(".")
            table = tables.get(table_id)
            if not table or column not in {
                str(col.get("name")) for col in table.get("columns") or []
            }:
                issues.append(f"关系引用了不存在的列: {ref}")
    for metric in card.get("metrics") or []:
        fact = str(metric.get("fact") or "")
        if fact and fact not in tables:
            issues.append(f"指标 {metric.get('id')} 的 fact 表不存在: {fact}")
        aggregation = str(metric.get("aggregation") or "").lower()
        operand = str(metric.get("operand") or "")
        if aggregation in {"avg", "sum"} and operand:
            table_id, _, column = operand.partition(".")
            table = tables.get(table_id) or {}
            target = next(
                (col for col in table.get("columns") or [] if str(col.get("name")) == column),
                None,
            )
            if target is None:
                issues.append(f"指标 {metric.get('id')} 的 operand 列不存在: {operand}")
            elif not is_numeric_type(target.get("type")):
                issues.append(
                    f"指标 {metric.get('id')} 的 {aggregation.upper()} 需要数值列，"
                    f"{operand} 是 {target.get('type')}"
                )
    return issues


def write_model_card(path: Path, card: dict[str, Any]) -> None:
    text = json.dumps(card, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_model_card(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not data or data.get("version") != 1:
        return {}
    return data


def build_model_card_for_context(ctx: Any) -> dict[str, Any]:
    """Build the card from a DatasetContext (used by publish/admin)."""
    overrides = load_overrides(Path(ctx.root))
    return build_model_card(
        dataset_id=ctx.dataset_id,
        sem=ctx.sem,
        catalog=ctx.catalog,
        profile=ctx.profile,
        overrides=overrides,
        documentation=ctx.documentation,
        data_version=ctx.data_version,
    )


def refresh_model_card(ctx: Any) -> dict[str, Any]:
    card = build_model_card_for_context(ctx)
    write_model_card(Path(ctx.root) / "model_card.json", card)
    return card
