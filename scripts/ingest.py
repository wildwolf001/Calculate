"""Rebuild the DuckDB warehouse and all generated artifacts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest.build_warehouse import build_warehouse  # noqa: E402


if __name__ == "__main__":
    info = build_warehouse()
    print(json.dumps(info, ensure_ascii=False, indent=2))
