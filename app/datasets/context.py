"""Immutable per-request view of one dataset.

Everything the controller needs to answer a question for a dataset lives here:
semantic layer, warehouse, value dictionary and quality report. Passing this
object around is what removes the global single-dataset assumption.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from app.domain.semantic.loader import SemanticLayer, load_semantic


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


@dataclass(frozen=True)
class DatasetContext:
    dataset_id: str
    name: str
    root: Path
    warehouse: Path
    semantic_path: Path
    binding_path: Path
    catalog_path: Path
    dim_values_path: Path
    quality_path: Path
    profile_path: Path
    mode: Literal["draft", "published"]
    data_version: str
    sem: SemanticLayer = field(repr=False)
    catalog: dict[str, Any] = field(repr=False, default_factory=dict)
    dim_values: dict[str, Any] = field(repr=False, default_factory=dict)
    quality: dict[str, Any] = field(repr=False, default_factory=dict)
    profile: dict[str, Any] = field(repr=False, default_factory=dict)
    source: dict[str, Any] = field(repr=False, default_factory=dict)
    documentation: dict[str, Any] = field(repr=False, default_factory=dict)
    readiness: dict[str, Any] = field(repr=False, default_factory=dict)
    capabilities: dict[str, Any] = field(repr=False, default_factory=dict)
    status: str = "draft"
    engine: str = "duckdb"

    @property
    def coverage(self) -> dict[str, Any] | None:
        return (self.capabilities or {}).get("member_list_coverage")

    @property
    def semantic_version(self) -> int:
        return self.sem.version

    @property
    def is_published(self) -> bool:
        return self.mode == "published"


def load_context(
    *,
    dataset_id: str,
    name: str,
    root: Path,
    warehouse: Path,
    semantic_path: Path,
    binding_path: Path,
    mode: Literal["draft", "published"],
    data_version: str,
    catalog_path: Path | None = None,
    dim_values_path: Path | None = None,
    quality_path: Path | None = None,
    profile_path: Path | None = None,
    source_path: Path | None = None,
    documentation_path: Path | None = None,
    readiness_path: Path | None = None,
    capabilities: dict[str, Any] | None = None,
    status: str = "draft",
    engine: str = "duckdb",
) -> DatasetContext:
    catalog_path = catalog_path or root / "catalog.json"
    dim_values_path = dim_values_path or root / "dim_values.json"
    quality_path = quality_path or root / "quality_report.json"
    profile_path = profile_path or root / "profile.json"
    source_path = source_path or root / "source.yaml"
    documentation_path = documentation_path or root / "documentation.json"
    readiness_path = readiness_path or root / "readiness.json"

    sem = load_semantic(semantic_path, binding_path, catalog_path=catalog_path)
    merged_caps = dict(sem.capabilities or {})
    merged_caps.update(capabilities or {})
    return DatasetContext(
        dataset_id=dataset_id,
        name=name,
        root=root,
        warehouse=warehouse,
        semantic_path=semantic_path,
        binding_path=binding_path,
        catalog_path=catalog_path,
        dim_values_path=dim_values_path,
        quality_path=quality_path,
        profile_path=profile_path,
        mode=mode,
        data_version=data_version,
        sem=sem,
        catalog=_read_json(catalog_path),
        dim_values=_read_json(dim_values_path),
        quality=_read_json(quality_path),
        profile=_read_json(profile_path),
        source=_read_yaml(source_path),
        documentation=_read_json(documentation_path),
        readiness=_read_json(readiness_path),
        capabilities=merged_caps,
        status=status,
        engine=engine,
    )
