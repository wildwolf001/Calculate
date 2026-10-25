"""Compare the current catalog against the previous run.

Drift is informational: added/removed tables and columns, row-count changes,
cardinality jumps. Ingest should fail loudly only when a *required* column
disappears (enforced by semantic layer validation, not here).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config.settings import CATALOG_PATH


def diff_catalogs(old: dict, new: dict) -> dict:
    old_t, new_t = old.get("tables", {}), new.get("tables", {})
    added_tables = sorted(set(new_t) - set(old_t))
    removed_tables = sorted(set(old_t) - set(new_t))
    changes = {}
    for table in sorted(set(old_t) & set(new_t)):
        oc = {c["name"]: c for c in old_t[table]["columns"]}
        nc = {c["name"]: c for c in new_t[table]["columns"]}
        added = sorted(set(nc) - set(oc))
        removed = sorted(set(oc) - set(nc))
        type_changes = sorted(
            n for n in set(oc) & set(nc) if oc[n]["type"] != nc[n]["type"]
        )
        row_delta = new_t[table]["rows"] - old_t[table]["rows"]
        if added or removed or type_changes or row_delta:
            changes[table] = {
                "added_columns": added,
                "removed_columns": removed,
                "type_changes": type_changes,
                "row_delta": row_delta,
            }
    return {"added_tables": added_tables, "removed_tables": removed_tables, "changes": changes}


def compare_with_previous(new_catalog: dict, catalog_path: Path = CATALOG_PATH) -> dict:
    if not catalog_path.exists():
        return {"first_run": True}
    try:
        old = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return {"first_run": True, "note": "previous catalog unreadable"}
    return diff_catalogs(old, new_catalog)
