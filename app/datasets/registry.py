"""Filesystem-backed dataset registry.

Every dataset is a self-contained package: its own semantic layer, binding,
catalog, value dictionary, quality report, profile, documentation and
readiness file. Runtime code never falls back to global e-commerce semantics.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config.settings import DATASETS_DIR, DEFAULT_DATASET_ID
from app.datasets.context import DatasetContext, load_context
from app.datasets.manifest import compute_data_version, read_manifest, write_manifest

DATASET_ID_RE = re.compile(r"^ds_[a-z0-9_-]{6,32}$")
SPEC_NAME = "dataset.json"


class DatasetError(RuntimeError):
    pass


class DatasetNotFound(DatasetError):
    pass


class DatasetUnavailable(DatasetError):
    pass


@dataclass(frozen=True)
class DatasetInfo:
    dataset_id: str
    name: str
    status: str
    rows: int | None
    tables: int | None
    semantic_version: int | None
    data_version: str | None
    updated_at: str | None
    readiness: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "status": self.status,
            "rows": self.rows,
            "tables": self.tables,
            "semantic_version": self.semantic_version,
            "data_version": self.data_version,
            "updated_at": self.updated_at,
            "readiness": self.readiness,
        }


def _read_spec(directory: Path) -> dict[str, Any]:
    path = directory / SPEC_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetUnavailable(f"无法读取 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DatasetUnavailable(f"{path} 必须是对象")
    return data


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _sum_rows(catalog: dict[str, Any]) -> int | None:
    tables = catalog.get("tables") or {}
    if not tables:
        return None
    total = 0
    for table in tables.values():
        rows = table.get("rows")
        if isinstance(rows, int):
            total += rows
    return total


class DatasetRegistry:
    def __init__(self, root: Path = DATASETS_DIR,
                 default_id: str = DEFAULT_DATASET_ID):
        self.root = Path(root)
        self.preferred_default = default_id
        self.default_id = ""
        self._dirs: dict[str, Path] = {}
        self._cache: dict[str, DatasetContext] = {}
        self._scan()

    # ---------- discovery ----------
    def _scan(self) -> None:
        self._dirs = {}
        if self.root.exists():
            for directory in sorted(self.root.iterdir()):
                if not directory.is_dir():
                    continue
                try:
                    spec = _read_spec(directory)
                    dataset_id = spec.get("dataset_id")
                    if not dataset_id:
                        continue
                    self._register_dir(dataset_id, directory)
                except DatasetError:
                    # A half-written dataset must not hide the others.
                    continue
        if self.preferred_default in self._dirs:
            self.default_id = self.preferred_default
        elif self._dirs:
            self.default_id = sorted(self._dirs)[0]
        else:
            self.default_id = ""

    def _register_dir(self, dataset_id: str, directory: Path) -> None:
        if not DATASET_ID_RE.match(dataset_id):
            raise DatasetUnavailable(f"非法 dataset_id: {dataset_id!r}")
        root = self.root.resolve()
        resolved = directory.resolve()
        if root != resolved and root not in resolved.parents:
            raise DatasetUnavailable(f"数据集目录越界: {directory}")
        self._dirs[dataset_id] = directory

    def refresh(self) -> None:
        self._cache.clear()
        self._scan()

    def invalidate(self, dataset_id: str | None = None) -> None:
        if dataset_id is None:
            self._cache.clear()
        else:
            self._cache.pop(dataset_id, None)

    # ---------- public ----------
    def ids(self) -> list[str]:
        return list(self._dirs)

    def exists(self, dataset_id: str | None) -> bool:
        return bool(dataset_id) and dataset_id in self._dirs

    def list(self) -> list[DatasetInfo]:
        infos: list[DatasetInfo] = []
        for dataset_id, directory in self._dirs.items():
            try:
                infos.append(self._info(dataset_id, directory))
            except DatasetError:
                continue
        return infos

    def _info(self, dataset_id: str, directory: Path) -> DatasetInfo:
        spec = _read_spec(directory)
        catalog = _read_json(directory / "catalog.json")
        manifest = read_manifest(directory)
        readiness = _read_json(directory / "readiness.json")
        return DatasetInfo(
            dataset_id=dataset_id,
            name=spec.get("name") or directory.name,
            status=spec.get("status") or "draft",
            rows=_sum_rows(catalog),
            tables=len(catalog.get("tables") or {}) or None,
            semantic_version=manifest.get("semantic_version"),
            data_version=manifest.get("data_version"),
            updated_at=manifest.get("generated_at") or spec.get("updated_at"),
            readiness=readiness.get("status"),
        )

    def get(self, dataset_id: str | None = None) -> DatasetContext:
        dataset_id = dataset_id or self.default_id
        if not dataset_id:
            raise DatasetNotFound("尚未导入数据源，请先在数据管理页上传数据")
        if not DATASET_ID_RE.match(dataset_id):
            raise DatasetNotFound(f"非法 dataset_id: {dataset_id!r}")
        cached = self._cache.get(dataset_id)
        if cached is not None:
            return cached
        directory = self._dirs.get(dataset_id)
        if directory is None:
            raise DatasetNotFound(f"数据源不存在: {dataset_id}")
        spec = _read_spec(directory)

        warehouse = directory / spec.get("warehouse", "warehouse.duckdb")
        if not warehouse.exists():
            raise DatasetUnavailable(
                f"数据源 {dataset_id} 尚未构建仓库，请先运行 ingest: {warehouse}"
            )

        semantic_path = directory / spec.get("semantic", "semantic.yaml")
        binding_path = directory / spec.get("binding", "binding.yaml")
        for path, label in ((semantic_path, "semantic"), (binding_path, "binding")):
            if not path.exists():
                raise DatasetUnavailable(f"数据源 {dataset_id} 缺少 {label}: {path}")

        manifest = read_manifest(directory)
        data_version = manifest.get("data_version") or compute_data_version(warehouse)
        status = str(spec.get("status", "draft"))

        ctx = load_context(
            dataset_id=dataset_id,
            name=spec.get("name") or directory.name,
            root=directory,
            warehouse=warehouse,
            semantic_path=semantic_path,
            binding_path=binding_path,
            mode="published" if status == "published" else "draft",
            data_version=str(data_version),
            catalog_path=directory / "catalog.json",
            dim_values_path=directory / "dim_values.json",
            quality_path=directory / "quality_report.json",
            profile_path=directory / "profile.json",
            source_path=directory / "source.yaml",
            documentation_path=directory / "documentation.json",
            readiness_path=directory / "readiness.json",
            capabilities=spec.get("capabilities") or {},
            status=status,
            engine=str(spec.get("engine", "duckdb")),
        )
        if not manifest.get("data_version") or manifest.get("semantic_version") != ctx.sem.version:
            manifest = {
                **manifest,
                "dataset_id": dataset_id,
                "data_version": data_version,
                "semantic_version": ctx.sem.version,
                "compiler_version": int(ctx.sem.raw.get("compiler_version", 1)),
            }
            try:
                write_manifest(directory, manifest)
            except OSError:
                pass
        self._cache[dataset_id] = ctx
        return ctx

    def default_context(self) -> DatasetContext:
        return self.get(self.default_id)


_registry: DatasetRegistry | None = None


def get_registry() -> DatasetRegistry:
    global _registry
    if _registry is None:
        _registry = DatasetRegistry()
    return _registry
