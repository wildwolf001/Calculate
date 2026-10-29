from __future__ import annotations

import json

from app.ingest.agent.service import run_ingest_agent


class FakeClient:
    available = True

    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = 0

    def complete_json(self, system, user, max_tokens=1200, stage="llm"):
        self.calls += 1
        return next(self.payloads)


def _write_csv(path):
    path.write_text(
        "order_id,channel,amount,status\n"
        "O1,线上,100,完成\n"
        "O2,线上,120,完成\n"
        "O3,线下,80,完成\n"
        "O4,线上,90,退单\n"
        "O5,线下,110,退单\n"
        "O6,线上,130,完成\n",
        encoding="utf-8",
    )


def test_ingest_agent_with_llm_decider(tmp_path):
    upload = tmp_path / "upload"
    out = tmp_path / "dataset"
    upload.mkdir()
    _write_csv(upload / "orders.csv")

    payloads = [
        {"kind": "tool", "tool": "files.list", "args": {}},
        {"kind": "tool", "tool": "table.profile", "args": {"file": "orders.csv"}},
        {"kind": "tool", "tool": "grain.detect",
         "args": {"table": "orders", "columns": ["order_id"]}},
        {"kind": "tool", "tool": "metric.propose", "args": {
            "id": "order_count", "name": "成交单数", "table": "orders",
            "aggregation": "COUNT_DISTINCT", "operand": "order_id",
            "scope": {"status": "完成"},
        }},
        {"kind": "tool", "tool": "metric.propose", "args": {
            "id": "refund_count", "name": "退单数量", "table": "orders",
            "aggregation": "COUNT_DISTINCT", "operand": "order_id",
            "scope": {"status": "退单"},
        }},
        {"kind": "tool", "tool": "metric.probe", "args": {"metric_id": "order_count"}},
        {"kind": "tool", "tool": "metric.probe", "args": {"metric_id": "refund_count"}},
        {"kind": "tool", "tool": "ratio.propose", "args": {
            "id": "refund_rate", "name": "退单率",
            "numerator": "refund_count", "denominator": "order_count",
        }},
        {"kind": "tool", "tool": "metric.probe", "args": {"metric_id": "refund_rate"}},
        {"kind": "finalize"},
    ]
    client = FakeClient(payloads)
    state = run_ingest_agent(upload, out, client)

    assert state.phase == "done"
    assert state.metrics["refund_rate"]["probe"]["value"] == 0.5
    assert client.calls == 10
    draft = json.loads((out / "ingest" / "draft.json").read_text(encoding="utf-8"))
    assert draft["metrics"]["refund_rate"]["provenance"] == "inferred"
