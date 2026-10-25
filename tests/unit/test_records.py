"""Query-record verification: reproduce, or explain why it cannot."""
from __future__ import annotations

from datetime import date

from app.domain.pipeline import execute_prepared, prepare
from app.domain.plan import QueryPlan, TimeRange
from app.domain.records import result_digest, verify_record


def make_artifact(sem):
    plan = QueryPlan(
        metric="gmv",
        time=TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1)),
        limit=10,
    )
    compiled, _ = prepare(plan, sem)
    result = execute_prepared(compiled)
    result.fingerprint = compiled.fingerprint(
        compiler_version=int(sem.raw.get("compiler_version", 1)),
        semantic_version=int(sem.version),
    )
    result.result_id = "r1-test"
    artifact = result.as_dict()
    artifact["plan"] = plan.model_dump(mode="json")
    return artifact


def test_verify_reproduces_stored_query(sem):
    artifact = make_artifact(sem)
    out = verify_record(artifact, sem)
    assert out["verdict"] == "reproduced"
    assert out["checks"]["sql_match"] is True
    assert out["checks"]["fingerprint_match"] is True
    assert out["checks"]["rows_match"] is True
    assert out["current"]["warehouse_version"]


def test_verify_detects_data_change(sem):
    artifact = make_artifact(sem)
    artifact["rows"][0][0] = float(artifact["rows"][0][0]) + 1
    out = verify_record(artifact, sem)
    assert out["verdict"] == "data_changed"
    assert out["checks"]["rows_match"] is False
    assert out["checks"]["sql_match"] is True


def test_verify_detects_sql_change(sem):
    artifact = make_artifact(sem)
    artifact["sql"] = artifact["sql"] + "\n-- tampered"
    out = verify_record(artifact, sem)
    assert out["verdict"] == "query_changed"
    assert out["checks"]["sql_match"] is False


def test_digest_is_order_insensitive(sem):
    cols = ["a"]
    assert result_digest(cols, [[1], [2]]) == result_digest(cols, [[2], [1]])
