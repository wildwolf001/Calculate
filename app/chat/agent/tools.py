from __future__ import annotations

from typing import Any

from app.agent.contracts import ToolContext, ToolResult


def _index(ctx: ToolContext) -> dict[str, Any]:
    return ctx.data.get("index") or {}


def _score(item: dict[str, Any], query: str) -> int:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("name", "definition", "expr", "numerator", "denominator")
    )
    text += " " + " ".join(str(v) for v in item.get("synonyms") or [])
    score = 0
    for token in query.split():
        if token and token in text:
            score += 1
    return score


class SearchSemanticsTool:
    name = "schema.search"
    description = "Search metrics and dimensions in the semantic index."
    input_schema = {"type": "object", "required": ["query"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        index = _index(ctx)
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 8)
        metrics = sorted(
            (index.get("metrics") or {}).items(),
            key=lambda item: (-_score(item[1], query), str(item[0])),
        )[:limit]
        dims = sorted(
            (index.get("dimensions") or {}).items(),
            key=lambda item: (-_score(item[1], query), str(item[0])),
        )[:limit]
        return ToolResult(
            facts={
                "metrics": [
                    {
                        "id": metric_id,
                        "name": metric.get("name"),
                        "type": metric.get("type"),
                        "expr": metric.get("expr") or (
                            f'{metric.get("numerator")} / {metric.get("denominator")}'
                        ),
                    }
                    for metric_id, metric in metrics
                ],
                "dimensions": [
                    {"id": dim_id, "name": dim.get("name"), "kind": dim.get("kind")}
                    for dim_id, dim in dims
                ],
            },
            summary=f"{len(metrics)} metrics / {len(dims)} dimensions",
        )


class DescribeMetricTool:
    name = "metric.describe"
    description = "Return one metric's expression, scope, time axis and dimensions."
    input_schema = {"type": "object", "required": ["metric_id"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        metric = (_index(ctx).get("metrics") or {}).get(str(args["metric_id"]))
        if metric is None:
            return ToolResult(ok=False, error="unknown metric")
        return ToolResult(facts={"metric": metric}, summary=str(metric.get("name")))


class ProfileColumnTool:
    name = "column.profile"
    description = "Return a column's type, null rate and samples from the index."
    input_schema = {"type": "object", "required": ["table", "column"]}

    def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        table = (_index(ctx).get("tables") or {}).get(str(args["table"])) or {}
        for column in table.get("columns") or []:
            if str(column.get("name")) == str(args["column"]):
                return ToolResult(facts={"column": column}, summary=str(column.get("name")))
        return ToolResult(ok=False, error="unknown column")
