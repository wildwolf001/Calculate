from __future__ import annotations

import json

from app.agent.contracts import Decision, ToolContext
from app.agent.controller import AgentController
from app.agent.events import JsonlEventLog
from app.ingest.agent.spec import ScriptedDecider, build_ingest_agent


def _write_csv(path, header, rows):
    lines = [",".join(header)]
    lines.extend(",".join(str(v) for v in row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ingest_agent_profiles_two_tables_and_probes_metrics(tmp_path):
    upload = tmp_path / "upload"
    out = tmp_path / "dataset"
    upload.mkdir()
    _write_csv(
        upload / "orders.csv",
        ["order_id", "channel", "amount", "status"],
        [
            ("O1", "线上", 100, "完成"),
            ("O2", "线上", 120, "完成"),
            ("O3", "线下", 80, "完成"),
            ("O4", "线上", 90, "退单"),
            ("O5", "线下", 110, "退单"),
            ("O6", "线上", 130, "完成"),
        ],
    )
    _write_csv(
        upload / "channels.csv",
        ["channel_id", "channel_name"],
        [("线上", "线上"), ("线下", "线下")],
    )

    decisions = [
        Decision(kind="tool", tool="files.list"),
        Decision(kind="tool", tool="table.profile", args={"file": "orders.csv"}),
        Decision(kind="tool", tool="table.profile", args={"file": "channels.csv"}),
        Decision(kind="tool", tool="grain.detect", args={"table": "orders", "columns": ["order_id"]}),
        Decision(kind="tool", tool="grain.detect", args={"table": "channels", "columns": ["channel_id"]}),
        Decision(kind="tool", tool="metric.propose", args={
            "id": "order_count", "name": "成交单数", "table": "orders",
            "aggregation": "COUNT_DISTINCT", "operand": "order_id",
            "scope": {"status": "完成"}, "unit": "单",
        }),
        Decision(kind="tool", tool="metric.propose", args={
            "id": "refund_count", "name": "退单数量", "table": "orders",
            "aggregation": "COUNT_DISTINCT", "operand": "order_id",
            "scope": {"status": "退单"}, "unit": "单",
        }),
        Decision(kind="tool", tool="metric.probe", args={"metric_id": "order_count"}),
        Decision(kind="tool", tool="metric.probe", args={"metric_id": "refund_count"}),
        Decision(kind="tool", tool="ratio.propose", args={
            "id": "refund_rate", "name": "退单率",
            "numerator": "refund_count", "denominator": "order_count",
        }),
        Decision(kind="tool", tool="metric.probe", args={"metric_id": "refund_rate"}),
        Decision(kind="finalize"),
    ]
    spec = build_ingest_agent(upload, out, ScriptedDecider(decisions))
    events = JsonlEventLog(out / "ingest" / "events.jsonl")
    ctx = ToolContext(data={"upload_dir": str(upload)})
    state = AgentController(spec, events, ctx=ctx).run()

    assert state.phase == "done"
    assert set(state.tables) == {"orders", "channels"}
    assert state.metrics["order_count"]["probe"]["value"] == 4
    assert state.metrics["refund_count"]["probe"]["value"] == 2
    assert state.metrics["refund_rate"]["probe"]["value"] == 0.5

    draft = json.loads((out / "ingest" / "draft.json").read_text(encoding="utf-8"))
    assert draft["metrics"]["refund_count"]["scope"] == {"status": "退单"}
    assert draft["metrics"]["refund_rate"]["expr"] == "refund_count / order_count"
    assert draft["tables"]["orders"]["grain"] == ["order_id"]

    types = [event.type for event in events.read()]
    assert "agent.completed" in types
    assert types.count("tool.result") == 11
