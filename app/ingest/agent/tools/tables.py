from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.agent.contracts import ToolContext, ToolResult


def _table_path(ctx: ToolContext, table: str) -> Path:
    state = ctx.state
    info = getattr(state, "tables", {}).get(table) or {}
    path = info.get("file") or f"{table}.csv"
    return Path(ctx.data["upload_dir"]) / path


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        return pd.read_excel(path, sheet_name=0)
    separator = "\t" if path.suffix.lower() == ".tsv" else ","
    return pd.read_csv(path, sep=separator)


def _kind(series: pd.Series) -> str:
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "text"


class TableProfileTool:
    name = "table.profile"
    description = "Read a CSV and profile its columns."
    input_schema = {"type": "object", "required": ["file"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = Path(ctx.data["upload_dir"]) / str(args["file"])
        frame = read_table(path)
        columns = []
        for name in frame.columns:
            series = frame[name]
            columns.append({
                "name": str(name),
                "kind": _kind(series),
                "nulls": int(series.isna().sum()),
                "distinct": int(series.nunique(dropna=True)),
                "samples": [str(v) for v in series.dropna().unique()[:3]],
            })
        table = {
            "name": path.stem,
            "file": path.name,
            "rows": int(len(frame)),
            "columns": columns,
        }
        return ToolResult(facts={"table": table}, summary=f"{table['name']}: {table['rows']} rows")


class GrainDetectTool:
    name = "grain.detect"
    description = "Check whether candidate columns uniquely identify rows."
    input_schema = {"type": "object", "required": ["table", "columns"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        table = str(args["table"])
        columns = [str(c) for c in args["columns"]]
        frame = read_table(_table_path(ctx, table))
        missing = [c for c in columns if c not in frame.columns]
        if missing:
            return ToolResult(ok=False, error=f"unknown columns: {missing}")
        duplicates = int(frame.duplicated(subset=columns).sum())
        return ToolResult(
            facts={
                "table": table,
                "grain": columns,
                "rows": int(len(frame)),
                "duplicates": duplicates,
                "unique": duplicates == 0,
            },
            summary=f"{table}: duplicates={duplicates}",
        )
