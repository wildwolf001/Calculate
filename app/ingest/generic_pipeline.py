"""End-to-end generic ingestion: files -> typed warehouse -> profile -> draft."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import duckdb
import yaml

from app.datasets.manifest import file_sha256, write_manifest
from app.domain.pipeline import execute_prepared, prepare
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import load_semantic
from app.ingest.adapters import discover_units, open_workbook, read_unit
from app.ingest.generic_types import (
    ColumnInfo,
    IngestOptions,
    ParsedDataset,
    TableInfo,
    jsonable,
)
from app.ingest.header import detect_header, find_totals_rows
from app.ingest.infer import duckdb_type, infer_column
from app.ingest.keys import detect_grain
from app.ingest.profile import build_catalog, build_profile
from app.ingest.quality_generic import run_generic_quality
from app.ingest.relations import RelationInfo, infer_relations
from app.ingest.semantic_draft import build_draft, write_draft

Progress = Callable[[str, int, str], None]

SUSPECTED_DOC_RE = re.compile(
    r"practice|question|readme|说明|练习题|题目|指标|口径|metrics?|dictionary|"
    r"codebook|schema|数据字典",
    re.IGNORECASE,
)


class IngestError(RuntimeError):
    pass


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def copy_sources(files: list[Path], root: Path) -> list[Path]:
    raw = root / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    used: set[str] = set()
    for path in files:
        name = Path(path).name
        target = raw / name
        stem, suffix = target.stem, target.suffix
        counter = 1
        while name in used or (target.exists() and target.resolve() != Path(path).resolve()):
            counter += 1
            name = f"{stem}_{counter}{suffix}"
            target = raw / name
        used.add(name)
        if target.resolve() != Path(path).resolve():
            shutil.copy2(path, target)
        copied.append(target)
    return copied


def _progress(callback: Progress | None, stage: str, percent: int, message: str) -> None:
    if callback is not None:
        callback(stage, percent, message)


class _WorkbookCache:
    """One openpyxl handle per source file, reused across its sheets."""

    def __init__(self) -> None:
        self._handles: dict[Path, Any] = {}

    def get(self, path: Path):
        workbook = self._handles.get(path)
        if workbook is None:
            workbook = open_workbook(path)
            self._handles[path] = workbook
        return workbook

    def close(self) -> None:
        for workbook in self._handles.values():
            try:
                workbook.close()
            except Exception:
                pass
        self._handles.clear()


# DuckDB's executemany() commits once per row, which turns ingestion into one
# WAL fsync per row. A single transaction plus multi-row VALUES keeps the same
# per-row conversion but lets DuckDB batch the write.
INSERT_PARAM_BUDGET = 20_000


def _insert_rows(con: duckdb.DuckDBPyConnection, table_id: str,
                 columns: list[ColumnInfo], rows: list[list[Any]],
                 progress: Callable[[int, int], None] | None = None) -> None:
    cols_sql = ", ".join(f"{_q(c.physical)} {duckdb_type(c.inferred_type)}"
                         for c in columns)
    placeholders = "(" + ", ".join("?" for _ in columns) + ")"
    chunk_size = max(1, INSERT_PARAM_BUDGET // max(1, len(columns)))
    total = len(rows)
    con.execute("BEGIN")
    try:
        con.execute(f"CREATE TABLE {_q(table_id)} ({cols_sql})")
        for start in range(0, total, chunk_size):
            chunk = rows[start:start + chunk_size]
            sql = (f"INSERT INTO {_q(table_id)} VALUES "
                   + ", ".join(placeholders for _ in chunk))
            con.execute(sql, [value for row in chunk for value in row])
            if progress is not None:
                progress(min(total, start + len(chunk)), total)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _populate_top_values(con: duckdb.DuckDBPyConnection,
                         table: TableInfo, show_pii: bool = False) -> None:
    for col in table.columns:
        if (col.is_pii and not show_pii) or col.distinct is None or col.distinct > 50:
            continue
        rows = con.execute(
            f"SELECT {_q(col.physical)}, COUNT(*) FROM {_q(table.table_id)} "
            f"WHERE {_q(col.physical)} IS NOT NULL GROUP BY 1 "
            f"ORDER BY 2 DESC LIMIT 5"
        ).fetchall()
        col.top_values = [{"value": jsonable(r[0]), "count": int(r[1])} for r in rows]


def _update_stats(table: TableInfo, inference_results: list) -> None:
    for col, result in zip(table.columns, inference_results):
        values = result.values
        non_null = [v for v in values if v is not None]
        col.null_count = len(values) - len(non_null)
        col.null_rate = round(col.null_count / len(values), 4) if values else 1.0
        col.distinct = len({str(v) for v in non_null}) if non_null else 0
        col.confidence = result.confidence
        col.unit = result.unit
        col.is_pii = result.is_pii
        col.samples = result.samples
        col.minimum = result.minimum
        col.maximum = result.maximum
        col.warnings = result.warnings


def _dim_values(con: duckdb.DuckDBPyConnection, semantic: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, list[Any]] = {}
    id_maps: dict[str, dict[str, Any]] = {}
    for dim_id, dim in (semantic.get("dimensions") or {}).items():
        if dim.get("kind") == "time" or not dim.get("value_source"):
            continue
        source = dim.get("value_source")
        display_ref = dim.get("column") or ""
        display_col = display_ref.split(".", 1)[1] if "." in display_ref else display_ref
        lookup_ref = dim.get("filter_lookup") or display_ref
        lookup_col = lookup_ref.split(".", 1)[1] if "." in lookup_ref else lookup_ref
        id_ref = dim.get("filter_column") or lookup_ref
        id_col = id_ref.split(".", 1)[1] if "." in id_ref else id_ref
        if not display_col or not lookup_col:
            continue
        try:
            rows = con.execute(
                f"SELECT DISTINCT {_q(lookup_col)} FROM {_q(source)} "
                f"WHERE {_q(lookup_col)} IS NOT NULL ORDER BY 1 LIMIT 5000"
            ).fetchall()
        except duckdb.Error:
            continue
        key = f"{source}.{display_col}"
        values[key] = [jsonable(r[0]) for r in rows]
        if id_col and id_col != lookup_col:
            try:
                pairs = con.execute(
                    f"SELECT {_q(id_col)}, {_q(lookup_col)} FROM {_q(source)} "
                    f"WHERE {_q(lookup_col)} IS NOT NULL LIMIT 50000"
                ).fetchall()
            except duckdb.Error:
                continue
            mapping: dict[str, Any] = {}
            for ident, label in pairs:
                mapping.setdefault(str(jsonable(label)), jsonable(ident))
            id_maps[dim_id] = mapping
    return {"values": values, "aliases": {}, "id_maps": id_maps}


def _relation_checks(con: duckdb.DuckDBPyConnection,
                     relations: list[RelationInfo]) -> list[dict[str, Any]]:
    orphans: dict[str, int] = {}
    amplified: dict[str, int] = {}
    for relation in relations:
        if not relation.accepted:
            continue
        key = f"{relation.child}.{relation.child_col} -> {relation.parent}.{relation.parent_col}"
        child = _q(relation.child)
        parent = _q(relation.parent)
        qcc = _q(relation.child_col)
        qpc = _q(relation.parent_col)
        orphan = con.execute(
            f"SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p "
            f"ON CAST(c.{qcc} AS VARCHAR) = CAST(p.{qpc} AS VARCHAR) "
            f"WHERE c.{qcc} IS NOT NULL AND p.{qpc} IS NULL"
        ).fetchone()[0]
        if orphan:
            orphans[key] = int(orphan)
        child_nonnull, joined = con.execute(
            f"SELECT COUNT({qcc}), (SELECT COUNT(*) FROM {child} c JOIN {parent} p "
            f"ON CAST(c.{qcc} AS VARCHAR) = CAST(p.{qpc} AS VARCHAR)) FROM {child}"
        ).fetchone()
        if int(joined) > int(child_nonnull):
            amplified[key] = int(joined) - int(child_nonnull)
    return [
        {
            "id": "relation_orphans",
            "severity": "warn" if orphans else "info",
            "title": "外键存在无法关联的孤儿值",
            "count": sum(orphans.values()),
            "detail": orphans,
        },
        {
            "id": "join_amplification",
            "severity": "error" if amplified else "info",
            "title": "连接后行数放大",
            "count": sum(amplified.values()),
            "detail": amplified,
        },
    ]


def _source_yaml(root: Path, dataset_id: str, name: str, files: list[Path],
                 tables: list[TableInfo], relations: list[RelationInfo],
                 options: IngestOptions, skipped: list[str]) -> dict[str, Any]:
    file_entries = []
    for path in files:
        file_entries.append({
            "path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
            "sha256": file_sha256(path),
            "size": path.stat().st_size,
        })
    return {
        "version": 1,
        "dataset_id": dataset_id,
        "name": name,
        "files": file_entries,
        "tables": {
            t.table_id: {
                "label": t.label,
                "source": t.source,
                "header_row": t.header_row,
                "grain": t.grain,
                "role": t.role,
                "columns": [
                    {"physical": c.physical, "label": c.label,
                     "type": c.inferred_type, "unit": c.unit,
                     "pii": c.is_pii}
                    for c in t.columns
                ],
            }
            for t in tables
        },
        "relationships": [r.as_dict() for r in relations if r.accepted],
        "options": options.as_dict(),
        "skipped_sources": skipped,
        "notes": [],
    }


def analyze_files(files: list[Path], root: Path, dataset_id: str, name: str,
                  progress: Progress | None = None,
                  validate_query: bool = True,
                  options: IngestOptions | None = None) -> ParsedDataset:
    options = options or IngestOptions()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    raw_files = copy_sources(files, root)
    if options.include_files:
        wanted = set(options.include_files)
        selected = [p for p in raw_files if p.name in wanted or p.stem in wanted]
        if not selected:
            raise IngestError("选择的文件中没有可解析的数据文件")
        raw_files = selected
    tmp_db = root / "warehouse.duckdb.tmp"
    if tmp_db.exists():
        tmp_db.unlink()
    final_db = root / "warehouse.duckdb"

    con = duckdb.connect(str(tmp_db))
    tables: list[TableInfo] = []
    dataset_warnings: list[str] = []
    workbooks = _WorkbookCache()      # no-op for CSV; one handle per Excel file
    try:
        _progress(progress, "discover", 5, "发现文件与工作表")
        all_units = discover_units(raw_files, workbook_for=workbooks.get)
        units = []
        skipped_sources: list[str] = []
        explicit_files = set(options.include_files or [])
        for unit in all_units:
            name_l = unit.file.name.lower()
            sheet_l = (unit.sheet or "").lower()
            suspected = bool(SUSPECTED_DOC_RE.search(name_l) or SUSPECTED_DOC_RE.search(sheet_l))
            explicitly = (unit.file.name in explicit_files or unit.label in explicit_files)
            if suspected and not options.include_suspected and not explicitly:
                skipped_sources.append(unit.label)
                continue
            units.append(unit)
        if not units:
            raise IngestError("没有可解析的 CSV/Excel 工作表（疑似说明文档已跳过）")
        if skipped_sources:
            dataset_warnings.append(
                "跳过疑似说明文档：" + "、".join(skipped_sources)
                + "（如需作为数据接入，请在高级选项中允许）")

        unit_count = len(units)
        for index, unit in enumerate(units, start=1):
            label = unit.label
            band_start = 10 + 30 * (index - 1) / unit_count
            band_end = 10 + 30 * index / unit_count
            span = band_end - band_start
            _progress(progress, "extract", int(band_start), f"读取 {label}")
            try:
                workbook = workbooks.get(unit.file) if unit.sheet is not None else None
                raw = read_unit(unit, workbook=workbook)
            except Exception as exc:
                dataset_warnings.append(f"{label}: 读取失败 {exc}")
                continue
            if not raw.rows:
                dataset_warnings.append(f"{label}: 工作表为空，已跳过")
                continue
            header = detect_header(raw.rows, raw.width)
            data_rows = raw.rows[header.index + 1:] if header.index is not None else raw.rows
            data_rows = [r for r in data_rows if any(str(c).strip() for c in r)]
            if not data_rows:
                dataset_warnings.append(f"{label}: 只有表头，没有数据行，已跳过")
                continue
            totals_indices = find_totals_rows(data_rows)
            totals_set = set(totals_indices)
            if totals_set and options.totals_policy == "exclude":
                data_rows = [r for i, r in enumerate(data_rows) if i not in totals_set]
                dataset_warnings.append(
                    f"{label}: 已从分析表排除 {len(totals_set)} 行合计/小计行（见质量报告）")
            elif totals_set:
                dataset_warnings.append(
                    f"{label}: 按配置保留了 {len(totals_set)} 行合计/小计行，注意重复计数")
            if not data_rows:
                dataset_warnings.append(f"{label}: 排除合计行后没有数据，已跳过")
                continue
            width = max(raw.width, len(header.labels), max(len(r) for r in data_rows))
            labels = list(header.labels) + [f"column_{i + 1}" for i in range(len(header.labels), width)]
            for row in data_rows:
                if len(row) < width:
                    row.extend([""] * (width - len(row)))

            _progress(progress, "extract", int(band_start + span * 0.08),
                      f"识别表头 {label}")
            table_id = f"t_{index:03d}"
            columns: list[ColumnInfo] = []
            inference_results = []
            converted_rows: list[list[Any]] = []
            for col_index in range(width):
                physical = f"c{col_index + 1:02d}"
                values = [row[col_index] for row in data_rows]
                result = infer_column(labels[col_index], values,
                                      mask_pii=(options.pii_policy == "mask"))
                inference_results.append(result)
                columns.append(ColumnInfo(
                    physical=physical,
                    label=labels[col_index],
                    index=col_index,
                    inferred_type=result.type,
                    confidence=result.confidence,
                    unit=result.unit,
                    is_pii=result.is_pii,
                    samples=result.samples,
                    minimum=result.minimum,
                    maximum=result.maximum,
                    warnings=list(result.warnings),
                ))
                _progress(
                    progress, "extract",
                    int(band_start + span * (0.08 + 0.62 * (col_index + 1) / width)),
                    f"推断类型 {label} 第 {col_index + 1}/{width} 列",
                )
            for row_index in range(len(data_rows)):
                converted_rows.append([
                    result.values[row_index] for result in inference_results
                ])

            def _row_progress(done: int, total: int, _start=band_start,
                              _span=span, _label=label) -> None:
                fraction = done / max(1, total)
                _progress(progress, "extract",
                          int(_start + _span * (0.70 + 0.30 * fraction)),
                          f"写入 {_label} {done}/{total} 行")

            _insert_rows(con, table_id, columns, converted_rows,
                         progress=_row_progress if progress is not None else None)
            table = TableInfo(
                table_id=table_id,
                label=label,
                source={
                    "file": str(unit.file.relative_to(root)) if unit.file.is_relative_to(root) else str(unit.file),
                    "sheet": unit.sheet,
                },
                rows=len(data_rows),
                columns=columns,
                header_row=header.index,
                skipped_rows=(header.index + 1) if header.index is not None else 0,
                totals_rows=totals_indices,
                warnings=list(raw.warnings) + list(header.warnings),
                encoding=raw.encoding,
                delimiter=raw.delimiter,
                hidden=unit.hidden,
            )
            _update_stats(table, inference_results)
            tables.append(table)

        if not tables:
            raise IngestError("未解析出任何有数据的表")

        _progress(progress, "keys", 45, "识别主键与粒度")
        for table in tables:
            grain = detect_grain(con, table.table_id,
                                 [(c.physical, c.label, c.inferred_type) for c in table.columns])
            table.grain = grain.grain
            table.duplicate_rows = grain.duplicate_rows
            for physical in table.grain:
                col = table.column(physical)
                if col:
                    col.role = "key"
            _populate_top_values(con, table, show_pii=(options.pii_policy == "show"))

        _progress(progress, "relations", 55, "推断表间关系")
        model = infer_relations(con, tables)
        for relation in model.relations:
            if relation.accepted:
                col = next((t for t in tables if t.table_id == relation.child), None)
                if col:
                    target = col.column(relation.child_col)
                    if target:
                        target.role = "fk"
        for table in tables:
            for col in table.columns:
                if col.role != "unknown":
                    continue
                if col.inferred_type in {"integer", "decimal"}:
                    col.role = "measure"
                elif col.distinct is not None and col.distinct <= 50:
                    col.role = "dimension"

        _progress(progress, "profile", 65, "生成画像与质量报告")
        catalog = build_catalog(con, tables,
                               show_pii_samples=(options.pii_policy == "show"))
        quality = run_generic_quality(
            con, tables, catalog,
            pii_policy=options.pii_policy,
            totals_policy=options.totals_policy,
        )
        quality["checks"].extend(_relation_checks(con, model.relations))
        profile = build_profile(name, tables, catalog, quality, options.as_dict())

        _progress(progress, "draft", 75, "生成语义草稿")
        semantic, binding, draft = build_draft(dataset_id, name, tables, model)
        semantic["generic"] = True
        dim_values = _dim_values(con, semantic)
        try:
            from app.ingest.llm_semantic import refine_semantic
            report = refine_semantic(
                semantic, draft, binding, tables, dim_values, con,
                progress=progress,
            )
            for warning in report.get("warnings") or []:
                dataset_warnings.append(f"模型口径：{warning}")
        except Exception as exc:
            dataset_warnings.append(
                f"模型口径未完成：{type(exc).__name__}: {exc}"
            )
        paths = write_draft(root, semantic, binding, draft)

        (root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / "dim_values.json").write_text(
            json.dumps(dim_values, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (root / "quality_report.json").write_text(
            json.dumps(quality, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        (root / "profile.json").write_text(
            json.dumps(profile, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        source = _source_yaml(root, dataset_id, name, raw_files, tables,
                              model.relations, options, skipped_sources)
        (root / "source.yaml").write_text(
            yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")

        _progress(progress, "artifacts", 85, "写入生成物")
    finally:
        workbooks.close()
        con.close()

    _progress(progress, "validate", 90, "校验语义草稿")
    sem = load_semantic(paths["semantic"], paths["binding"],
                        catalog_path=root / "catalog.json")
    if validate_query:
        metric_id = next(iter(semantic.get("metrics") or {}), None)
        if metric_id:
            plan = QueryPlan(metric=metric_id, dimensions=[], limit=10)
            compiled, _ = prepare(plan, sem)
            execute_prepared(compiled, db_path=tmp_db)

    if tmp_db.exists():
        tmp_db.replace(final_db)

    data_version = "sha256:" + file_sha256(final_db)[:16]
    manifest = {
        "dataset_id": dataset_id,
        "name": name,
        "data_version": data_version,
        "semantic_version": 1,
        "compiler_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator_version": "generic-m3",
        "options": options.as_dict(),
        "source_files": [
            {"path": str(p.relative_to(root)), "sha256": file_sha256(p)}
            for p in raw_files
        ],
        "tables": [{"table_id": t.table_id, "rows": t.rows,
                    "columns": len(t.columns)} for t in tables],
        "quality_summary": {
            "error": sum(1 for q in quality["checks"] if q["severity"] == "error"),
            "warn": sum(1 for q in quality["checks"] if q["severity"] == "warn"),
            "info": sum(1 for q in quality["checks"] if q["severity"] == "info"),
        },
    }
    write_manifest(root, manifest)
    (root / "dataset.json").write_text(json.dumps({
        "dataset_id": dataset_id,
        "name": name,
        "status": "draft",
        "engine": "duckdb",
        "semantic": "semantic.yaml",
        "binding": "binding.yaml",
        "warehouse": "warehouse.duckdb",
        "capabilities": semantic.get("capabilities") or {},
        "options": options.as_dict(),
        "skipped_sources": skipped_sources,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        from app.datasets.context import load_context
        from app.datasets.readiness import build_readiness, write_readiness
        ctx = load_context(
            dataset_id=dataset_id,
            name=name,
            root=root,
            warehouse=final_db,
            semantic_path=paths["semantic"],
            binding_path=paths["binding"],
            mode="draft",
            data_version=data_version,
            capabilities=semantic.get("capabilities") or {},
            status="draft",
        )
        selected_metrics = [
            m["id"] for m in draft.get("metrics", []) if m.get("selected", True)
        ]
        selected_dims = [
            d["id"] for d in draft.get("dimensions", []) if d.get("selected", True)
        ]
        write_readiness(root, build_readiness(ctx, selected_metrics, selected_dims))
    except Exception as exc:
        # Readiness is a gate, not a parsing failure: record the problem and
        # let the user see it before publishing.
        dataset_warnings.append(f"就绪检查未完成: {type(exc).__name__}: {exc}")

    _progress(progress, "done", 100, "解析完成")
    return ParsedDataset(
        dataset_id=dataset_id,
        name=name,
        tables=tables,
        files=source["files"],
        warnings=dataset_warnings,
        generated_at=manifest["generated_at"],
    )
