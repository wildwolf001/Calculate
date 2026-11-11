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

from app.datasets.index import load_index, semantic_fingerprint
from app.datasets.model_card import (
    build_model_card,
    load_model_card,
    load_overrides,
    model_card_fingerprint,
)
from app.domain.model.model import SemanticModel
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
    model: SemanticModel | None = field(default=None, repr=False)
    status: str = "draft"
    engine: str = "duckdb"
    index_path: Path | None = None
    index: dict[str, Any] = field(default_factory=dict, repr=False)
    model_card_path: Path | None = None
    model_card: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def coverage(self) -> dict[str, Any] | None:
        return (self.capabilities or {}).get("member_list_coverage")

    @property
    def semantic_version(self) -> int:
        return self.sem.version

    @property
    def is_published(self) -> bool:
        return self.mode == "published"

    @property
    def model_version(self) -> str | None:
        return self.model.version if self.model is not None else None


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
    index_path = root / "semantic_index.json"

    sem = load_semantic(semantic_path, binding_path, catalog_path=catalog_path)
    merged_caps = dict(sem.capabilities or {})
    merged_caps.update(capabilities or {})
    try:
        model = SemanticModel.from_layer(sem)
    except Exception:
        model = None
    catalog = _read_json(catalog_path)
    dim_values = _read_json(dim_values_path)
    documentation = _read_json(documentation_path)
    profile = _read_json(profile_path)
    # An index that does not describe the YAML we just loaded is worse than no
    # index: the parser would brief the model on renamed/removed objects.
    index = load_index(index_path)
    if index:
        expected = index.get("semantic_fingerprint")
        if expected:
            if expected != semantic_fingerprint(sem):
                index = {}
        elif str(index.get("semantic_version")) != str(sem.version):
            index = {}
    if not index:
        # Datasets published before the index existed have none on disk. Build
        # one in memory so the parser still sees column labels, samples and
        # supported dimensions; it is persisted on the next publish.
        try:
            from app.datasets.index import build_index

            index = build_index(
                dataset_id=dataset_id,
                data_version=data_version,
                semantic_version=sem.version,
                sem=sem,
                catalog=catalog,
                dim_values=dim_values,
                documentation=documentation,
                capabilities=merged_caps,
            )
        except Exception:
            index = {}
    model_card_path = root / "model_card.json"
    overrides = load_overrides(root)
    card = load_model_card(model_card_path)
    expected_sem, expected_over = model_card_fingerprint(sem, overrides)
    if (
        not card
        or str(card.get("semantic_fingerprint")) != expected_sem
        or str(card.get("overrides_hash")) != expected_over
    ):
        try:
            card = build_model_card(
                dataset_id=dataset_id,
                sem=sem,
                catalog=catalog,
                profile=profile,
                overrides=overrides,
                documentation=documentation,
                data_version=data_version,
            )
        except Exception:
            card = {}
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
        catalog=catalog,
        dim_values=dim_values,
        quality=_read_json(quality_path),
        profile=profile,
        source=_read_yaml(source_path),
        documentation=documentation,
        readiness=_read_json(readiness_path),
        capabilities=merged_caps,
        model=model,
        status=status,
        engine=engine,
        index_path=index_path,
        index=index,
        model_card_path=model_card_path,
        model_card=card,
    )
