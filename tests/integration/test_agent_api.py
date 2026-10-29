from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.api import uploads as uploads_module
from app.main import app


class FakeClient:
    available = True

    def __init__(self, payloads):
        self.payloads = iter(payloads)

    def complete_json(self, system, user, max_tokens=1200, stage="llm"):
        return next(self.payloads)


def _csv():
    return (
        "order_id,channel,amount,status\n"
        "O1,线上,100,完成\n"
        "O2,线上,120,完成\n"
        "O3,线下,80,完成\n"
        "O4,线上,90,退单\n"
        "O5,线下,110,退单\n"
        "O6,线上,130,完成\n"
    ).encode("utf-8")


def _payloads():
    return [
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


def test_agent_profile_job_and_sse(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads_module, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(uploads_module, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(uploads_module, "DATASETS_DIR", tmp_path / "datasets")
    monkeypatch.setattr(main_module, "JOBS_DIR", tmp_path / "jobs")

    with TestClient(app) as client:
        app.state.agent_client = FakeClient(_payloads())
        response = client.post(
            "/api/uploads",
            files=[("files", ("orders.csv", _csv(), "text/csv"))],
        )
        assert response.status_code == 200, response.text
        upload_id = response.json()["upload_id"]

        response = client.post(f"/api/uploads/{upload_id}/agent-profile")
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        out_dir = Path(response.json()["out_dir"])

        deadline = time.time() + 20
        record = {}
        while time.time() < deadline:
            record = client.get(f"/api/jobs/{job_id}").json()
            if record.get("status") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert record.get("status") == "succeeded", record
        assert record["result"]["metrics"]["refund_rate"]["probe"]["value"] == 0.5

        events = client.get(f"/api/jobs/{job_id}/events")
        assert events.status_code == 200
        assert "event: done" in events.text
        assert "tool.result" in events.text
        assert "agent.completed" in events.text

        review = client.get(f"/api/jobs/{job_id}/review")
        assert review.status_code == 200
        assert "refund_rate" in review.json()["draft"]["metrics"]

        saved = client.post(
            f"/api/jobs/{job_id}/review",
            json=[{"item_id": "refund_rate", "action": "accept", "note": "口径正确"}],
        )
        assert saved.status_code == 200
        assert saved.json()["decisions"][0]["action"] == "accept"
        assert client.get(f"/api/jobs/{job_id}/review").json()["review"]["decisions"]

        applied = client.post(f"/api/jobs/{job_id}/review/apply")
        assert applied.status_code == 200, applied.text
        assert "refund_rate" in applied.json()["accepted"]
        candidate = json.loads(
            (out_dir / "ingest" / "semantic_draft.json").read_text(encoding="utf-8")
        )
        assert [m["id"] for m in candidate["metrics"] if m["id"] == "refund_rate"]

        materialized = client.post(f"/api/jobs/{job_id}/materialize", json={})
        assert materialized.status_code == 200, materialized.text
        dataset_root = Path(materialized.json()["root"])
        assert (dataset_root / "semantic.yaml").exists()
        assert (dataset_root / "warehouse.duckdb").exists()

        from app.datasets.context import load_context

        manifest = json.loads((dataset_root / "manifest.json").read_text(encoding="utf-8"))
        ctx = load_context(
            dataset_id=materialized.json()["dataset_id"],
            name="agent-test",
            root=dataset_root,
            warehouse=dataset_root / "warehouse.duckdb",
            semantic_path=dataset_root / "semantic.yaml",
            binding_path=dataset_root / "binding.yaml",
            mode="draft",
            data_version=manifest.get("data_version", "sha256:test"),
        )
        assert "refund_rate" in ctx.sem.metrics
        assert "order_count" in ctx.sem.metrics

        from app.core.state import Slots
        from app.domain.pipeline import execute_prepared, prepare
        from app.domain.planner import plan_from_slots

        plan = plan_from_slots(Slots(metric="refund_rate"), ctx.sem)
        compiled, _ = prepare(plan, ctx.sem)
        result = execute_prepared(
            compiled, db_path=dataset_root / "warehouse.duckdb"
        )
        assert result.rows[0][2] == 0.5
