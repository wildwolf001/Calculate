"""Generate a *draft* semantic layer from the catalog for human review.

Never overwrites semantic.yaml. The draft contains only physical facts
(tables, columns, PK candidates, cardinality); business meaning still has to
be confirmed by a human.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.config.settings import CATALOG_PATH, DATASET_DIR


def build_draft(catalog_path: Path = CATALOG_PATH) -> dict:
    if not catalog_path.exists():
        raise FileNotFoundError("catalog.json 不存在，请先运行 scripts/ingest.py")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    draft = {"version": 0, "dataset": "draft", "tables": {}, "candidate_dimensions": {}}
    for table, info in catalog["tables"].items():
        cols = info["columns"]
        pk_candidates = [c["name"] for c in cols if c["unique"]]
        draft["tables"][table] = {
            "grain": pk_candidates[:1],
            "rows": info["rows"],
            "columns": {c["name"]: c["type"] for c in cols},
        }
        for c in cols:
            if 2 <= c["distinct"] <= 50 and c.get("samples"):
                draft["candidate_dimensions"][f"{table}.{c['name']}"] = c["samples"][:10]
    return draft


def write_draft(out_path: Path | None = None) -> Path:
    draft = build_draft()
    out_path = out_path or (DATASET_DIR / "semantic_draft.yaml")
    out_path.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    return out_path


if __name__ == "__main__":
    print(write_draft())
