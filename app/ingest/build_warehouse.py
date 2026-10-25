"""CSV / Excel -> DuckDB warehouse. Idempotent: rebuilds from raw each run."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pandas as pd

from app.config.settings import CATALOG_PATH, DIM_VALUES_PATH, QUALITY_PATH, RAW_DIR, WAREHOUSE_PATH
from app.ingest.catalog import build_catalog
from app.ingest.catalogs import build_dim_values
from app.ingest.drift import compare_with_previous
from app.ingest.normalize import TABLE_SPECS, normalize_table
from app.ingest.quality import run_quality_checks

# Sheets that exist in the Excel workbook but are documentation, not data.
NON_DATA_SHEETS = {"说明", "练习题", "README", "Metric"}


def _read_excel(path: Path) -> dict[str, pd.DataFrame]:
    book = pd.read_excel(path, sheet_name=None)
    return {name: df for name, df in book.items() if name not in NON_DATA_SHEETS}


def _read_csv_dir(path: Path) -> dict[str, pd.DataFrame]:
    return {p.stem: pd.read_csv(p) for p in sorted(path.glob("*.csv")) if p.stem != "practice_questions"}


def load_raw(raw_dir: Path = RAW_DIR) -> tuple[dict[str, pd.DataFrame], str]:
    xlsx = sorted(raw_dir.glob("*.xlsx"))
    if xlsx:
        return _read_excel(xlsx[0]), xlsx[0].name
    csv_dirs = [p for p in raw_dir.iterdir() if p.is_dir() and any(p.glob("*.csv"))]
    if csv_dirs:
        return _read_csv_dir(csv_dirs[0]), str(csv_dirs[0])
    raise FileNotFoundError(f"no .xlsx or csv directory under {raw_dir}")


def build_warehouse(raw_dir: Path = RAW_DIR, db_path: Path = WAREHOUSE_PATH) -> dict:
    frames, source = load_raw(raw_dir)
    logs = []
    normalized: dict[str, pd.DataFrame] = {}
    for name in TABLE_SPECS:
        if name not in frames:
            raise KeyError(f"raw source {source} is missing sheet/table {name!r}")
        df, log = normalize_table(name, frames[name])
        normalized[name] = df
        logs.append(log)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".duckdb.tmp")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    try:
        for name, df in normalized.items():
            con.register("_df", df)
            con.execute(f'CREATE TABLE "{name}" AS SELECT * FROM _df')
            con.unregister("_df")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    if db_path.exists():
        db_path.unlink()
    shutil.move(str(tmp), str(db_path))

    catalog = build_catalog(db_path)
    drift = compare_with_previous(catalog)
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    dim_values = build_dim_values(db_path)
    DIM_VALUES_PATH.write_text(json.dumps(dim_values, ensure_ascii=False, indent=2), encoding="utf-8")

    quality = run_quality_checks(db_path)
    quality["source"] = source
    quality["drift"] = drift
    quality["normalize"] = [log.__dict__ for log in logs]
    QUALITY_PATH.write_text(json.dumps(quality, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {
        "source": source,
        "db": str(db_path),
        "tables": {k: len(v) for k, v in normalized.items()},
        "quality_issues": len(quality.get("checks", [])),
    }
