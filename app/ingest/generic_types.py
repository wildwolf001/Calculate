"""Shared dataclasses for generic tabular ingestion (CSV/XLSX)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return value


@dataclass
class IngestOptions:
    """User-visible ingestion policies. Defaults are recommendations, not law."""
    totals_policy: str = "exclude"      # exclude | keep
    pii_policy: str = "mask"            # mask | show
    include_files: list[str] | None = None
    include_suspected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "totals_policy": self.totals_policy,
            "pii_policy": self.pii_policy,
            "include_files": self.include_files,
            "include_suspected": self.include_suspected,
        }


@dataclass
class ColumnInfo:
    physical: str
    label: str
    index: int
    inferred_type: str = "varchar"
    confidence: float = 0.0
    null_count: int = 0
    null_rate: float = 0.0
    distinct: int | None = None
    samples: list[Any] = field(default_factory=list)
    unit: str | None = None
    warnings: list[str] = field(default_factory=list)
    role: str = "unknown"          # key | fk | measure | dimension | unknown
    is_pii: bool = False
    minimum: Any = None
    maximum: Any = None
    top_values: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("samples", "minimum", "maximum"):
            data[key] = jsonable(data[key])
        data["top_values"] = [
            {"value": jsonable(v.get("value")), "count": v.get("count")}
            for v in self.top_values
        ]
        return data


@dataclass
class TableInfo:
    table_id: str
    label: str
    source: dict[str, Any]
    rows: int = 0
    columns: list[ColumnInfo] = field(default_factory=list)
    header_row: int | None = None
    skipped_rows: int = 0
    grain: list[str] = field(default_factory=list)
    duplicate_rows: int = 0
    totals_rows: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    encoding: str | None = None
    delimiter: str | None = None
    hidden: bool = False
    role: str = "unknown"          # fact | dimension | standalone

    @property
    def type(self) -> str:
        return "table"

    def column(self, physical: str) -> ColumnInfo | None:
        for col in self.columns:
            if col.physical == physical:
                return col
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_id": self.table_id,
            "label": self.label,
            "source": self.source,
            "rows": self.rows,
            "columns": [c.as_dict() for c in self.columns],
            "header_row": self.header_row,
            "skipped_rows": self.skipped_rows,
            "grain": self.grain,
            "duplicate_rows": self.duplicate_rows,
            "totals_rows": self.totals_rows,
            "warnings": self.warnings,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "hidden": self.hidden,
            "role": self.role,
        }


@dataclass
class ParsedDataset:
    dataset_id: str
    name: str
    tables: list[TableInfo] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "name": self.name,
            "tables": [t.as_dict() for t in self.tables],
            "files": self.files,
            "warnings": self.warnings,
            "generated_at": self.generated_at,
        }
