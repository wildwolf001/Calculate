"""Publish a reviewed semantic draft.

Publishing is explicit and atomic: filter the draft, validate it against the
warehouse, run one probe query, then replace the published YAML. A failure
leaves the previous version untouched.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import yaml

from app.datasets.manifest import read_manifest, write_manifest
from app.datasets.registry import DatasetContext, DatasetError, DatasetRegistry
from app.domain.pipeline import execute_prepared, prepare
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import load_semantic
from app.domain.semantic.timeaxis import infer_time_axis


class PublishError(DatasetError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PublishError(f"{path} 不是对象")
    return data


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def publish_dataset(registry: DatasetRegistry, dataset_id: str,
                    selected_metrics: list[str] | None = None,
                    selected_dimensions: list[str] | None = None) -> dict[str, Any]:
    ctx: DatasetContext = registry.get(dataset_id)
    if ctx.mode == "published":
        # Re-publishing is allowed (a new version), but the draft is the source
        # of candidate IDs.
        pass
    semantic_path = ctx.semantic_path
    binding_path = ctx.binding_path
    semantic = _read_yaml(semantic_path)
    binding = _read_yaml(binding_path)

    metrics = dict(semantic.get("metrics") or {})
    dimensions = dict(semantic.get("dimensions") or {})
    if not metrics:
        raise PublishError("语义草稿没有指标，无法发布")

    draft_path = ctx.root / "semantic_draft.json"
    draft: dict[str, Any] = {}
    if draft_path.exists():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            draft = {}

    if selected_metrics is None:
        selected_metrics = [
            m["id"] for m in draft.get("metrics", [])
            if m.get("selected", True) and m.get("id") in metrics
        ] or list(metrics)
    if selected_dimensions is None:
        selected_dimensions = [
            d["id"] for d in draft.get("dimensions", [])
            if d.get("selected", True) and d.get("id") in dimensions
        ] or list(dimensions)

    keep_metrics = list(selected_metrics)
    unknown_metrics = [m for m in keep_metrics if m not in metrics]
    if unknown_metrics:
        raise PublishError(f"未知指标: {unknown_metrics}")
    if not keep_metrics:
        raise PublishError("至少选择一个指标")

    keep_dims = list(selected_dimensions)
    unknown_dims = [d for d in keep_dims if d not in dimensions]
    if unknown_dims:
        raise PublishError(f"未知维度: {unknown_dims}")
    # A metric's supported_grain is a contract: publishing must not silently
    # drop a dimension the metric declares it can be split by.
    referenced_dims = {
        grain
        for metric_id in keep_metrics
        for grain in (metrics[metric_id].get("supported_grain") or [])
        if grain not in {"none", "month"} and grain in dimensions
    }
    keep_dims = list(dict.fromkeys([*keep_dims, *sorted(referenced_dims)]))
    if "month" in dimensions and any(
        "month" in (metrics[m].get("supported_grain") or []) for m in keep_metrics
    ) and "month" not in keep_dims:
        keep_dims.append("month")

    new_metrics: dict[str, Any] = {}
    for metric_id in keep_metrics:
        raw = json.loads(json.dumps(metrics[metric_id]))
        grains = raw.get("supported_grain") or ["none"]
        raw["supported_grain"] = [g for g in grains if g in {"none", *keep_dims}]
        raw["provenance"] = "human"
        raw["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
        definition = str(raw.get("definition") or "")
        definition = re.sub(r"（自动口径[^）]*）", "", definition).strip()
        if definition:
            raw["definition"] = definition
        new_metrics[metric_id] = raw

    def _axis_for(ref: str) -> str | None:
        table, _, column = str(ref).partition(".")
        columns = (
            ((ctx.catalog.get("tables") or {}).get(table) or {}).get("columns")
        ) or []
        for col in columns:
            if str(col.get("name")) == column:
                return infer_time_axis(col.get("label") or column)
        return None

    for raw in new_metrics.values():
        time_via = raw.get("time_via")
        if isinstance(time_via, str) and time_via:
            raw["time_axis"] = _axis_for(time_via) or raw.get("time_axis")

    try:
        from app.datasets.health import build_metric_health

        health = build_metric_health(
            ctx.sem, list(new_metrics), ctx.warehouse, catalog=ctx.catalog,
        )
        for metric_id, item in health.items():
            if metric_id in new_metrics:
                new_metrics[metric_id]["data_health"] = item
    except Exception:
        # Health is advisory; never block a publish because of it.
        pass

    new_dimensions: dict[str, Any] = {}
    for dim_id in keep_dims:
        raw = json.loads(json.dumps(dimensions[dim_id]))
        raw["provenance"] = "human"
        raw["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
        new_dimensions[dim_id] = raw

    version = int(semantic.get("version", 1)) + 1
    semantic["version"] = version
    semantic["dataset"] = ctx.name
    semantic["dataset_id"] = dataset_id
    semantic["status"] = "published"
    semantic["provenance"] = "human"
    semantic["published_at"] = datetime.now().isoformat(timespec="seconds")
    semantic["metrics"] = new_metrics
    semantic["dimensions"] = new_dimensions

    reverse_binding = {
        tid: {str(phys): str(logical) for logical, phys in (cols or {}).items()}
        for tid, cols in (binding.get("tables") or {}).items()
    }
    time_axes: list[dict[str, Any]] = []
    coverage_con = None
    try:
        coverage_con = duckdb.connect(str(ctx.warehouse), read_only=True)
    except Exception:
        coverage_con = None
    try:
        for tid, table in (ctx.catalog.get("tables") or {}).items():
            for col in table.get("columns") or []:
                if str(col.get("type")) not in {"datetime", "date"}:
                    continue
                physical = str(col.get("name"))
                logical = reverse_binding.get(tid, {}).get(physical, physical)
                label = col.get("label") or physical
                coverage = None
                if coverage_con is not None:
                    try:
                        lo, hi = coverage_con.execute(
                            f'SELECT MIN("{physical}"), MAX("{physical}") '
                            f'FROM "{tid}"'
                        ).fetchone()
                        if lo is not None and hi is not None:
                            coverage = [str(lo)[:10], str(hi)[:10]]
                    except Exception:
                        coverage = None
                time_axes.append({
                    "ref": f"{tid}.{logical}",
                    "label": str(label),
                    "name_zh": infer_time_axis(str(label)) or str(label),
                    "coverage": coverage,
                })
    finally:
        if coverage_con is not None:
            coverage_con.close()
    semantic["time_axes"] = time_axes

    history = ctx.root / "history"
    history.mkdir(exist_ok=True)
    backup = history / f"semantic-v{version - 1}.yaml"
    if semantic_path.exists() and not backup.exists():
        shutil.copy2(semantic_path, backup)

    old_text = semantic_path.read_text(encoding="utf-8")
    new_text = yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False)
    _write_atomic(semantic_path, new_text)

    try:
        loaded = load_semantic(semantic_path, binding_path,
                               catalog_path=ctx.catalog_path)
        metric_id = next(iter(loaded.metrics))
        plan = QueryPlan(metric=metric_id, dimensions=[], limit=5)
        compiled, _ = prepare(plan, loaded)
        execute_prepared(compiled, db_path=ctx.warehouse)
    except Exception as exc:
        _write_atomic(semantic_path, old_text)
        raise PublishError(f"发布校验失败，已回滚: {type(exc).__name__}: {exc}") from exc

    try:
        from app.datasets.readiness import build_readiness, write_readiness
        registry.invalidate(dataset_id)
        probe_ctx = registry.get(dataset_id)
        readiness = build_readiness(probe_ctx, keep_metrics, keep_dims)
        if readiness.get("status") == "blocked":
            _write_atomic(semantic_path, old_text)
            detail = "；".join(readiness.get("blockers", [])[:5])
            raise PublishError(f"就绪检查未通过，已回滚: {detail}")
        write_readiness(ctx.root, readiness)
    except PublishError:
        raise
    except Exception as exc:
        _write_atomic(semantic_path, old_text)
        raise PublishError(f"就绪检查失败，已回滚: {type(exc).__name__}: {exc}") from exc

    try:
        from dataclasses import replace

        from app.domain.model.model import SemanticModel
        from app.domain.model.validate import validate_semantics
        from app.domain.semantic.graph import build_capabilities
        from app.domain.semantic.loader import Dimension, Metric

        cap_sem = replace(
            ctx.sem,
            metrics={
                mid: Metric(mid, raw) for mid, raw in new_metrics.items()
            },
            dimensions={
                did: Dimension(did, raw) for did, raw in new_dimensions.items()
            },
        )
        capabilities = build_capabilities(cap_sem)
        semantic_issues = [
            issue.as_dict()
            for issue in validate_semantics(SemanticModel.from_layer(cap_sem))
        ]
        capability_payload = {
            mid: capability.as_dict()
            for mid, capability in capabilities.items()
        }
        try:
            from app.datasets.index import build_index, write_index

            index = build_index(
                dataset_id=dataset_id,
                data_version=ctx.data_version,
                semantic_version=version,
                sem=cap_sem,
                catalog=ctx.catalog,
                dim_values=ctx.dim_values,
                documentation=ctx.documentation,
                capabilities=capability_payload,
            )
            write_index(ctx.root / "semantic_index.json", index)
        except Exception:
            # The index is rebuildable; never fail a publish on it.
            pass
        _write_atomic(
            ctx.root / "capabilities.json",
            json.dumps({
                "dataset_id": dataset_id,
                "semantic_version": version,
                "generated_at": semantic["published_at"],
                "metrics": {
                    mid: capability.as_dict()
                    for mid, capability in capabilities.items()
                },
                "semantic_issues": semantic_issues,
            }, ensure_ascii=False, indent=2),
        )
    except Exception:
        # Capability metadata is advisory; never fail a publish on it.
        pass

    descriptor_path = ctx.root / "dataset.json"
    descriptor = {}
    if descriptor_path.exists():
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            descriptor = {}
    descriptor.update({
        "dataset_id": dataset_id,
        "status": "published",
        "semantic": semantic_path.name,
        "binding": binding_path.name,
        "warehouse": ctx.warehouse.name,
        "engine": descriptor.get("engine", "duckdb"),
        "capabilities": semantic.get("capabilities") or descriptor.get("capabilities") or {},
        "published_at": semantic["published_at"],
    })
    _write_atomic(descriptor_path, json.dumps(descriptor, ensure_ascii=False, indent=2))

    manifest = read_manifest(ctx.root)
    manifest.update({
        "dataset_id": dataset_id,
        "semantic_version": version,
        "status": "published",
        "updated_at": semantic["published_at"],
        "data_version": ctx.data_version,
    })
    write_manifest(ctx.root, manifest)

    draft_path = ctx.root / "semantic_draft.json"
    if draft_path.exists():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft.update({
                "status": "published",
                "selected_metrics": keep_metrics,
                "selected_dimensions": keep_dims,
                "published_at": semantic["published_at"],
            })
            _write_atomic(draft_path, json.dumps(draft, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            pass

    registry.invalidate(dataset_id)
    return {
        "dataset_id": dataset_id,
        "status": "published",
        "semantic_version": version,
        "metrics": keep_metrics,
        "dimensions": keep_dims,
        "warnings": [],
    }
