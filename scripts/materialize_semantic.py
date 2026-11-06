"""One-time build-time migration: write entity registry + generated metrics.

Usage:
  python scripts/materialize_semantic.py data/datasets/<id>
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.semantic.loader import materialize_semantic  # noqa: E402


def main(root: Path) -> None:
    semantic_path = root / "semantic.yaml"
    data = yaml.safe_load(semantic_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"not a semantic object: {semantic_path}")
    migrated = materialize_semantic(
        data,
        data.get("metrics") or {},
        data.get("dimensions") or {},
        keep_dims=list((data.get("dimensions") or {}).keys()),
    )
    backup = semantic_path.with_suffix(".yaml.before-entities")
    if not backup.exists():
        shutil.copy2(semantic_path, backup)
    semantic_path.write_text(
        yaml.safe_dump(migrated, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"materialized {semantic_path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: materialize_semantic.py <dataset-root>")
    main(Path(sys.argv[1]).resolve())
