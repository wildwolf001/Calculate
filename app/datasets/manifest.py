"""Dataset manifest: which source produced the warehouse and which version it is.

The manifest is what makes a warehouse snapshot auditable. It is written once
per build; readers must not silently assume an artifact is current.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def compute_data_version(warehouse: Path) -> str:
    if not warehouse.exists():
        return "missing"
    return "sha256:" + file_sha256(warehouse)[:16]


def read_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_manifest(root: Path, payload: dict[str, Any]) -> Path:
    """Atomic write: readers see either the old manifest or the new one."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / MANIFEST_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def ensure_manifest(dataset_id: str, root: Path, warehouse: Path,
                    semantic_version: int) -> dict[str, Any]:
    """Return an existing manifest or create one for a pre-existing warehouse."""
    existing = read_manifest(root)
    if existing and existing.get("data_version"):
        return existing
    payload = {
        "dataset_id": dataset_id,
        "data_version": compute_data_version(warehouse),
        "semantic_version": semantic_version,
        "compiler_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator_version": "m0-registry",
        "warehouse": warehouse.name,
    }
    write_manifest(root, payload)
    return payload
