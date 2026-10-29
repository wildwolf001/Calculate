from __future__ import annotations

from pathlib import Path

from app.agent.contracts import ToolContext, ToolResult


class ListFilesTool:
    name = "files.list"
    description = "List CSV files in the upload directory."
    input_schema = {"type": "object", "required": []}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        root = Path(ctx.data["upload_dir"])
        files = []
        for path in sorted(root.iterdir()):
            if path.suffix.lower() not in {".csv", ".tsv", ".xlsx", ".xlsm"}:
                continue
            files.append({
                "name": path.name,
                "path": str(path),
                "size": path.stat().st_size,
            })
        return ToolResult(facts={"files": files}, summary=f"{len(files)} files")
