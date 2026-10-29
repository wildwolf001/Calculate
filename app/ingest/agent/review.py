"""Apply human review decisions to an ingest-agent draft.

The output is a *candidate* semantic draft, not a published dataset: it has
tables, metrics and dimensions, but no physical warehouse/binding yet.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dimensions(tables: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for table_id, table in tables.items():
        for column in table.get("columns") or []:
            name = str(column.get("name"))
            distinct = column.get("distinct")
            if column.get("kind") != "text":
                continue
            if not isinstance(distinct, int) or not 2 <= distinct <= 50:
                continue
            out.append({
                "id": f"d_{table_id}_{name}",
                "name_zh": name,
                "kind": "categorical",
                "column": f"{table_id}.{name}",
                "selected": True,
                "provenance": "inferred",
            })
    return out


def apply_review(ingest_dir: Path) -> dict[str, Any]:
    ingest_dir = Path(ingest_dir)
    draft = _load(ingest_dir / "draft.json")
    review = _load(ingest_dir / "review.json")
    decisions = {
        str(item.get("item_id")): str(item.get("action") or "accept")
        for item in (review.get("decisions") or [])
        if isinstance(item, dict)
    }
    tables = draft.get("tables") or {}
    source_metrics = draft.get("metrics") or {}
    metrics: list[dict[str, Any]] = []
    accepted: list[str] = []
    rejected: list[str] = []
    for metric_id, metric in source_metrics.items():
        action = decisions.get(metric_id)
        if action == "reject":
            rejected.append(metric_id)
            continue
        if action not in {None, "accept"}:
            continue
        accepted.append(metric_id)
        metrics.append({
            "id": metric_id,
            "name_zh": metric.get("name") or metric_id,
            "name": metric.get("name") or metric_id,
            "fact": metric.get("table"),
            "expr": metric.get("expr"),
            "aggregation": metric.get("aggregation"),
            "type": "ratio" if metric.get("type") == "ratio" else "aggregate",
            "numerator": metric.get("numerator"),
            "denominator": metric.get("denominator"),
            "scope": metric.get("scope") or {},
            "unit": metric.get("unit"),
            "selected": True,
            "provenance": "inferred",
            "confidence": 0.6,
        })

    payload = {
        "version": 1,
        "source": "ingest_agent",
        "tables": [
            {
                "table_id": table_id,
                "label": table_id,
                "rows": table.get("rows"),
                "grain": table.get("grain") or [],
                "columns": [
                    {
                        "physical": column.get("name"),
                        "label": column.get("name"),
                        "type": column.get("kind"),
                    }
                    for column in table.get("columns") or []
                ],
            }
            for table_id, table in tables.items()
        ],
        "metrics": metrics,
        "dimensions": _dimensions(tables),
        "relations": [],
        "selected_metrics": accepted,
        "selected_dimensions": [],
    }
    (ingest_dir / "semantic_draft.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    approved = {
        "accepted": accepted,
        "rejected": rejected,
        "draft": str(ingest_dir / "semantic_draft.json"),
    }
    (ingest_dir / "approved_metrics.json").write_text(
        json.dumps(approved, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        **approved,
        "publishable": False,
        "message": "已生成候选语义草稿；还需要物理仓库/binding 才能发布。",
    }
