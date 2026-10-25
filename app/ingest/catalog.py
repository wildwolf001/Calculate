"""Physical catalog: row counts, column types, PK candidates, cardinality, samples.

The catalog is a generated artifact. It describes what is actually in the
warehouse; it never encodes business meaning (that lives in semantic.yaml).
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.config.settings import WAREHOUSE_PATH

# Columns whose distinct values are safe and useful to sample.
SAMPLE_MAX_CARDINALITY = 60


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def build_catalog(db_path: Path = WAREHOUSE_PATH) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        out: dict = {"tables": {}}
        for table in tables:
            qt = _quote(table)
            rows = con.execute(f"SELECT COUNT(*) FROM {qt}").fetchone()[0]
            cols = con.execute(f"PRAGMA table_info({qt})").fetchall()
            described = []
            for _cid, name, dtype, *_ in cols:
                qc = _quote(name)
                nulls = con.execute(f"SELECT COUNT(*) FROM {qt} WHERE {qc} IS NULL").fetchone()[0]
                distinct = con.execute(f"SELECT COUNT(DISTINCT {qc}) FROM {qt}").fetchone()[0]
                info = {
                    "name": name,
                    "type": dtype,
                    "nulls": nulls,
                    "null_rate": round(nulls / rows, 4) if rows else None,
                    "distinct": distinct,
                    "unique": distinct == rows and rows > 0,
                }
                if distinct <= SAMPLE_MAX_CARDINALITY:
                    vals = con.execute(
                        f"SELECT DISTINCT {qc} FROM {qt} WHERE {qc} IS NOT NULL ORDER BY 1 LIMIT 20"
                    ).fetchall()
                    info["samples"] = [v[0].isoformat() if hasattr(v[0], "isoformat") else v[0] for v in vals]
                described.append(info)
            out["tables"][table] = {"rows": rows, "columns": described}
        return out
    finally:
        con.close()
