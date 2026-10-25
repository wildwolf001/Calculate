from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def sse_events(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        name = None
        data = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if not data:
            continue
        payload = json.loads("".join(data))
        payload.setdefault("type", name or "message")
        events.append(payload)
    return events


def new_session(client) -> str:
    resp = client.post("/api/sessions", json={})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def test_meta(client):
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset"] == "ecommerce_agent_demo"
    assert any(m["id"] == "gmv" for m in body["metrics"])
    assert body["llm"]["enabled"] is False


def test_turn_stream_and_state(client):
    sid = new_session(client)
    resp = client.post(f"/api/sessions/{sid}/turns", json={
        "message": "2024年1月至2025年12月已送达订单的商品销售额是多少？不含运费。"
    })
    assert resp.status_code == 200
    events = sse_events(resp.text)
    types = [e["type"] for e in events]
    assert "sql.executed" in types
    assert "answer.composed" in types
    assert types[-1] == "done"

    state = client.get(f"/api/sessions/{sid}").json()
    assert state["status"] == "DONE"
    artifact = client.get(f"/api/sessions/{sid}/results/{state['last_result_id']}").json()
    assert artifact["rows"][0][0] == pytest.approx(4858920.84, abs=0.01)
    assert "SELECT" in artifact["sql"].upper() or "WITH" in artifact["sql"].upper()


def test_analysis_modules_catalog(client):
    data = client.get("/api/analysis/modules").json()
    modules = {m["id"]: m for m in data["modules"]}
    assert {"trend", "structure", "driver", "fulfillment", "payment",
            "reputation", "customer", "seller", "product"} <= set(modules)
    assert modules["seller"]["default"] is False
    assert modules["trend"]["default"] is True


def test_analysis_hints_catalog(client):
    data = client.get("/api/analysis/hints").json()
    assert "数码家电" in data["categories"]
    assert data["products"]
    assert data["time_range"]


def test_analysis_endpoint_with_selected_modules(client):
    sid = new_session(client)
    resp = client.post(f"/api/sessions/{sid}/analysis",
                       json={"sections": ["fulfillment"]})
    assert resp.status_code == 200
    events = sse_events(resp.text)
    requested = next(e for e in events if e["type"] == "analysis.requested")
    assert requested["payload"]["modules"] == ["fulfillment"]
    executed = [e for e in events if e["type"] == "sql.executed"]
    assert len(executed) == 1
    report = client.get(f"/api/sessions/{sid}").json()["analysis"]["report"]
    assert "**本次关注**：履约" in report
    assert "规模与趋势" not in report


def test_analysis_endpoint(client):
    sid = new_session(client)
    resp = client.post(f"/api/sessions/{sid}/analysis")
    assert resp.status_code == 200
    events = sse_events(resp.text)
    types = [e["type"] for e in events]
    assert "analysis.requested" in types
    assert "analysis.completed" in types
    assert "sql.executed" in types
    assert "answer.composed" in types

    state = client.get(f"/api/sessions/{sid}").json()
    assert state["status"] == "DONE"
    assert state["analysis"]["status"] == "done"
    assert "经营分析报告" in state["analysis"]["report"]


def test_history_record_projection_and_verify(client):
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/turns", json={"message": "5月的订单数是多少？"})

    body = client.get(f"/api/sessions/{sid}/history").json()
    assert body["semantic_version"]
    assert body["warehouse_version"]
    record = body["history"][0]
    for key in ("sql", "params", "fingerprint", "plan", "preview", "checks",
                "status", "warehouse_version", "kind", "turn"):
        assert key in record
    assert record["plan"]["metric"] == "order_count"
    assert record["status"] == "ok"
    assert record["preview"]

    resp = client.post(f"/api/sessions/{sid}/records/{record['result_id']}/verify")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"] == "reproduced"
    assert data["checks"] == {
        "sql_match": True,
        "fingerprint_match": True,
        "columns_match": True,
        "rows_match": True,
    }


def test_verify_unknown_record_returns_404(client):
    sid = new_session(client)
    resp = client.post(f"/api/sessions/{sid}/records/r999-missing/verify")
    assert resp.status_code == 404


def test_history_and_export(client):
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/turns", json={"message": "5月的订单数是多少？"})
    hist = client.get(f"/api/sessions/{sid}/history").json()["history"]
    assert len(hist) == 1
    rid = hist[0]["result_id"]
    csv_resp = client.get(f"/api/sessions/{sid}/results/{rid}/export.csv")
    assert csv_resp.status_code == 200
    assert "value" in csv_resp.text.splitlines()[0]


def test_unknown_session_returns_404(client):
    assert client.get("/api/sessions/does-not-exist").status_code == 404
    assert client.post("/api/sessions/does-not-exist/turns",
                       json={"message": "hi"}).status_code == 404


def test_clarification_over_api(client):
    sid = new_session(client)
    resp = client.post(f"/api/sessions/{sid}/turns", json={"message": "珠宝首饰的销售额是多少？"})
    events = sse_events(resp.text)
    assert any(e["type"] == "ambiguity.detected" for e in events)
    state = client.get(f"/api/sessions/{sid}").json()
    assert state["status"] == "AWAITING_CLARIFICATION"

    resp = client.post(f"/api/sessions/{sid}/clarifications", json={"value": "数码家电"})
    events = sse_events(resp.text)
    assert any(e["type"] == "sql.executed" for e in events)
    state = client.get(f"/api/sessions/{sid}").json()
    assert state["status"] == "DONE"
    assert state["slots"]["filters"]["category"]["value"] in (["electronics"], "electronics")


def test_validation_error_on_empty_message(client):
    sid = new_session(client)
    assert client.post(f"/api/sessions/{sid}/turns", json={"message": ""}).status_code == 422
