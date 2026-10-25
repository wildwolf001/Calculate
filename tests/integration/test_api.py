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
