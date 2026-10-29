from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class IngestArtifacts:
    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)

    def write(self, state: Any) -> dict[str, str]:
        target = self.out_dir / "ingest"
        target.mkdir(parents=True, exist_ok=True)
        draft = {
            "version": 1,
            "phase": state.phase,
            "tables": state.tables,
            "metrics": state.metrics,
            "issues": state.issues,
        }
        draft_path = target / "draft.json"
        draft_path.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        issues_path = target / "issues.json"
        issues_path.write_text(
            json.dumps(state.issues, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"draft": str(draft_path), "issues": str(issues_path)}
