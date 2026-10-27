"""M3/M4: upload -> analyze job -> explore -> publish -> delete."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

import app.api.routes as routes_module
import app.api.uploads as uploads_module
from app.core.jobs import JobManager
from app.main import app

CSV = (
    "order_id,customer_id,order_date,amount\n"
    "1,C1,2025-01-03,100\n"
    "2,C2,2025-01-04,50\n"
    "3,C1,2025-02-03,25\n"
).encode("utf-8")


def _wait_job(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_upload_analyze_explore_publish_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads_module, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(uploads_module, "DATASETS_DIR", tmp_path / "datasets")
    monkeypatch.setattr(routes_module, "TRASH_DIR", tmp_path / "trash")

    with TestClient(app) as client:
        service = app.state.service
        service.registry.root = tmp_path / "datasets"
        service.registry.refresh()
        app.state.jobs = JobManager(tmp_path / "jobs")

        resp = client.post("/api/uploads", files=[
            ("files", ("orders.csv", CSV, "text/csv")),
        ])
        assert resp.status_code == 200, resp.text
        upload_id = resp.json()["upload_id"]

        resp = client.post(f"/api/uploads/{upload_id}/analyze",
                           json={"name": "上传测试"})
        assert resp.status_code == 200, resp.text
        started = resp.json()
        job = _wait_job(client, started["job_id"])
        assert job["status"] == "succeeded", job
        dataset_id = started["dataset_id"]

        listing = client.get("/api/datasets").json()
        assert dataset_id in {item["dataset_id"] for item in listing["datasets"]}
        detail = client.get(f"/api/datasets/{dataset_id}").json()
        assert detail["mode"] == "draft"
        assert detail["capabilities"]["member_list"] is True
        readiness = client.get(f"/api/datasets/{dataset_id}/readiness").json()
        assert readiness["status"] == "ready"

        profile = client.get(f"/api/datasets/{dataset_id}/profile").json()
        assert profile["overview"]["rows"] == 3
        assert "数据源包含" in profile["summary"]

        draft = client.get(f"/api/datasets/{dataset_id}/draft").json()
        assert draft["status"] == "draft"
        row_metric = next(m["id"] for m in draft["metrics"]
                          if m["id"].endswith("row_count"))

        sid = client.post("/api/sessions", json={"dataset_id": dataset_id}).json()["session_id"]
        stream = client.post(f"/api/sessions/{sid}/turns",
                             json={"message": "行数是多少？"}).text
        assert "自动口径" in stream
        assert "3" in stream

        resp = client.post(f"/api/datasets/{dataset_id}/publish",
                           json={"metrics": [row_metric]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["semantic_version"] == 2
        assert client.get(f"/api/datasets/{dataset_id}").json()["mode"] == "published"

        stream = client.post(f"/api/sessions/{sid}/turns",
                             json={"message": "行数是多少？"}).text
        assert "自动口径" not in stream
        assert "3" in stream

        resp = client.delete(f"/api/datasets/{dataset_id}")
        assert resp.status_code == 200, resp.text
        assert client.get(f"/api/datasets/{dataset_id}").status_code == 404


def test_documentation_edit_publish_and_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads_module, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(uploads_module, "DATASETS_DIR", tmp_path / "datasets")
    monkeypatch.setattr(routes_module, "TRASH_DIR", tmp_path / "trash")
    with TestClient(app) as client:
        service = app.state.service
        service.registry.root = tmp_path / "datasets"
        service.registry.refresh()
        app.state.jobs = JobManager(tmp_path / "jobs")

        resp = client.post("/api/uploads", files=[
            ("files", ("sales.csv", CSV, "text/csv")),
        ])
        upload_id = resp.json()["upload_id"]
        started = client.post(f"/api/uploads/{upload_id}/analyze",
                              json={"name": "文档测试"}).json()
        job = _wait_job(client, started["job_id"])
        assert job["status"] == "succeeded", job
        dataset_id = started["dataset_id"]

        doc = "销售额：amount 合计。\n订单数：按 order_id 去重。\n"
        resp = client.post(
            f"/api/datasets/{dataset_id}/documentation",
            files=[("file", ("README.md", doc.encode("utf-8"), "text/markdown"))],
        )
        assert resp.status_code == 200, resp.text
        parsed = resp.json()
        assert {"销售额", "订单数"} <= {r["name"] for r in parsed["rules"]}

        resp = client.post(f"/api/datasets/{dataset_id}/documentation/apply")
        assert resp.status_code == 200, resp.text
        applied = resp.json()
        assert applied["applied"]
        assert client.get(f"/api/datasets/{dataset_id}").json()["mode"] == "draft"

        draft = client.get(f"/api/datasets/{dataset_id}/draft").json()
        names = {m.get("name_zh") for m in draft["metrics"]}
        assert {"销售额", "订单数"} <= names
        sales_id = next(m["id"] for m in draft["metrics"]
                        if m.get("name_zh") == "销售额")

        resp = client.patch(f"/api/datasets/{dataset_id}/draft", json={
            "metrics": [{"id": sales_id, "name_zh": "销售额（已确认）"}],
        })
        assert resp.status_code == 200, resp.text

        resp = client.post(f"/api/datasets/{dataset_id}/publish",
                           json={"metrics": [sales_id]})
        assert resp.status_code == 200, resp.text
        sid = client.post("/api/sessions",
                          json={"dataset_id": dataset_id}).json()["session_id"]
        stream = client.post(f"/api/sessions/{sid}/turns",
                             json={"message": "销售额（已确认）是多少"}).text
        assert "175" in stream

        resp = client.post(f"/api/datasets/{dataset_id}/rebuild",
                           json={"totals_policy": "exclude", "pii_policy": "mask"})
        assert resp.status_code == 200, resp.text
        rebuilt = _wait_job(client, resp.json()["job_id"], timeout=60)
        assert rebuilt["status"] == "succeeded", rebuilt
        assert client.get(f"/api/datasets/{dataset_id}").json()["mode"] == "draft"


def test_empty_project_api_starts_without_data(tmp_path, monkeypatch):
    with TestClient(app) as client:
        service = app.state.service
        service.registry.root = tmp_path / "empty"
        service.registry.refresh()
        app.state.jobs = JobManager(tmp_path / "jobs")
        listing = client.get("/api/datasets")
        assert listing.status_code == 200
        assert listing.json()["datasets"] == []
        assert client.get("/api/meta").status_code == 404
        assert client.post("/api/sessions", json={}).status_code == 404


def test_upload_rejects_unsupported_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads_module, "UPLOADS_DIR", tmp_path / "uploads")
    with TestClient(app) as client:
        app.state.jobs = JobManager(tmp_path / "jobs")
        resp = client.post("/api/uploads", files=[
            ("files", ("notes.pdf", b"%PDF-1.4", "application/pdf")),
        ])
        assert resp.status_code == 400


def test_job_manager_progress_and_cancel(tmp_path):
    manager = JobManager(tmp_path / "jobs")

    def work(*, progress):
        progress("extract", 30, "half")
        return {"ok": True}

    job_id = manager.submit("test", work)
    record = manager.wait(job_id, timeout=10)
    assert record["status"] == "succeeded"
    assert record["result"] == {"ok": True}
    assert record["percent"] == 100

    def slow(*, progress):
        progress("extract", 10, "start")
        time.sleep(0.3)
        progress("extract", 80, "late")
        return {"ok": True}

    job_id = manager.submit("test", slow)
    time.sleep(0.05)
    manager.cancel(job_id)
    record = manager.wait(job_id, timeout=10)
    assert record["status"] in {"cancelled", "succeeded"}
