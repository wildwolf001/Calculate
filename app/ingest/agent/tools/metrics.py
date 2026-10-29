from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.agent.contracts import ToolContext, ToolResult
from app.ingest.agent.tools.tables import read_table

_AGG = {"COUNT", "COUNT_DISTINCT", "SUM", "AVG"}


class MetricProposeTool:
    name = "metric.propose"
    description = "Register a metric candidate against a profiled table."
    input_schema = {"type": "object", "required": ["id", "table", "aggregation", "operand"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        table = getattr(ctx.state, "tables", {}).get(str(args["table"]))
        if table is None:
            return ToolResult(ok=False, error="table not profiled")
        aggregation = str(args["aggregation"]).upper()
        if aggregation not in _AGG:
            return ToolResult(ok=False, error=f"unsupported aggregation: {aggregation}")
        operand = str(args["operand"])
        if operand not in {c["name"] for c in table.get("columns") or []}:
            return ToolResult(ok=False, error=f"unknown column: {operand}")
        expr = (
            f"COUNT(DISTINCT {operand})"
            if aggregation == "COUNT_DISTINCT"
            else f"{aggregation}({operand})"
        )
        metric = {
            "id": str(args["id"]),
            "name": str(args.get("name") or args["id"]),
            "table": table["name"],
            "aggregation": aggregation,
            "operand": operand,
            "expr": expr,
            "scope": dict(args.get("scope") or {}),
            "unit": args.get("unit"),
            "provenance": "inferred",
        }
        return ToolResult(facts={"metric": metric}, summary=f"metric {metric['id']}")


class RatioProposeTool:
    name = "ratio.propose"
    description = "Register a ratio of two proposed metrics (e.g. refund / completed)."
    input_schema = {"type": "object", "required": ["id", "numerator", "denominator"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        metrics = getattr(ctx.state, "metrics", {})
        numerator = metrics.get(str(args["numerator"]))
        denominator = metrics.get(str(args["denominator"]))
        if numerator is None or denominator is None:
            return ToolResult(ok=False, error="ratio operands must be proposed first")
        metric = {
            "id": str(args["id"]),
            "name": str(args.get("name") or args["id"]),
            "type": "ratio",
            "table": numerator.get("table"),
            "numerator": numerator["id"],
            "denominator": denominator["id"],
            "expr": f"{numerator['id']} / {denominator['id']}",
            "scope": {},
            "unit": args.get("unit") or "%",
            "provenance": "inferred",
        }
        return ToolResult(facts={"metric": metric}, summary=f"ratio {metric['id']}")


class MetricProbeTool:
    name = "metric.probe"
    description = "Execute a metric candidate with pandas; no arbitrary SQL."
    input_schema = {"type": "object", "required": ["metric_id"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        metric = getattr(ctx.state, "metrics", {}).get(str(args["metric_id"]))
        if metric is None:
            return ToolResult(ok=False, error="metric not proposed")
        if metric.get("type") == "ratio":
            metrics = getattr(ctx.state, "metrics", {})
            numerator = (metrics.get(metric["numerator"]) or {}).get("probe") or {}
            denominator = (metrics.get(metric["denominator"]) or {}).get("probe") or {}
            if numerator.get("value") is None or not denominator.get("value"):
                return ToolResult(ok=False, error="ratio operands have no probe")
            value = float(numerator["value"]) / float(denominator["value"])
            return ToolResult(
                facts={"metric_id": metric["id"], "value": value, "rows": None},
                summary=f"{metric['id']}={value:.4f}",
            )
        table = getattr(ctx.state, "tables", {}).get(metric["table"])
        if table is None:
            return ToolResult(ok=False, error="table not profiled")
        path = Path(ctx.data["upload_dir"]) / table.get("file", f"{table['name']}.csv")
        frame = read_table(path)
        for column, value in (metric.get("scope") or {}).items():
            if column not in frame.columns:
                return ToolResult(ok=False, error=f"unknown scope column: {column}")
            frame = frame[frame[column].astype(str) == str(value)]
        operand = metric["operand"]
        aggregation = metric["aggregation"]
        if operand not in frame.columns:
            return ToolResult(ok=False, error=f"unknown operand: {operand}")
        if aggregation == "COUNT_DISTINCT":
            value = frame[operand].nunique(dropna=True)
        elif aggregation == "COUNT":
            value = int(frame[operand].count())
        elif aggregation == "SUM":
            value = float(pd.to_numeric(frame[operand], errors="coerce").sum())
        else:
            value = float(pd.to_numeric(frame[operand], errors="coerce").mean())
        return ToolResult(
            facts={"metric_id": metric["id"], "value": value, "rows": int(len(frame))},
            summary=f"{metric['id']}={value}",
        )
