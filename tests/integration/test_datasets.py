"""M0: dataset registry, session binding and per-dataset context isolation."""
from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app.config.settings import (
    BINDING_PATH,
    CATALOG_PATH,
    DATASET_DIR,
    DIM_VALUES_PATH,
    QUALITY_PATH,
    SEMANTIC_PATH,
)
from app.datasets.registry import DatasetNotFound, DatasetRegistry
from app.main import app

_DATA_AVAILABLE = (
    (DATASET_DIR / "dataset.json").exists()
    and (DATASET_DIR / "warehouse.duckdb").exists()
)
pytestmark = pytest.mark.skipif(
    not _DATA_AVAILABLE, reason="未导入默认数据源"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_registry_exposes_default_dataset():
    registry = DatasetRegistry()
    assert registry.default_id in registry.ids()
    ctx = registry.get()
    assert ctx.dataset_id == registry.default_id
    assert ctx.warehouse.exists()
    assert len(ctx.sem.metrics) >= 10
    assert ctx.catalog.get("tables")


def _make_dataset(root, dataset_id: str) -> None:
    directory = root / dataset_id
    directory.mkdir(parents=True)
    for src, name in (
        (SEMANTIC_PATH, "semantic.yaml"),
        (BINDING_PATH, "binding.yaml"),
        (CATALOG_PATH, "catalog.json"),
        (DIM_VALUES_PATH, "dim_values.json"),
        (QUALITY_PATH, "quality_report.json"),
    ):
        shutil.copy(src, directory / name)
    # The registry only checks existence here; no query runs in this test.
    (directory / "warehouse.duckdb").write_bytes(b"")
    (directory / "dataset.json").write_text(
        json.dumps({
            "dataset_id": dataset_id,
            "name": dataset_id,
            "status": "published",
            "semantic": "semantic.yaml",
            "binding": "binding.yaml",
        }),
        encoding="utf-8",
    )


def test_two_datasets_have_isolated_contexts(tmp_path):
    _make_dataset(tmp_path, "ds_dataset_one")
    _make_dataset(tmp_path, "ds_dataset_two")
    registry = DatasetRegistry(root=tmp_path, default_id="ds_dataset_one",
                               default_dir=tmp_path / "ds_dataset_one")
    one = registry.get("ds_dataset_one")
    two = registry.get("ds_dataset_two")
    assert one.dataset_id != two.dataset_id
    assert one.warehouse != two.warehouse
    assert one.root != two.root
    assert one.sem is not two.sem
    with pytest.raises(DatasetNotFound):
        registry.get("ds_missing_dataset")


def test_session_records_dataset_id(client):
    resp = client.post("/api/sessions", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_id"] == "ds_ecommerce_demo"
    state = client.get(f"/api/sessions/{body['session_id']}").json()
    assert state["dataset_id"] == "ds_ecommerce_demo"


def test_unknown_dataset_is_rejected(client):
    resp = client.post("/api/sessions", json={"dataset_id": "ds_unknown_dataset"})
    assert resp.status_code == 404


def test_dataset_endpoints(client):
    listing = client.get("/api/datasets")
    assert listing.status_code == 200
    ids = {item["dataset_id"] for item in listing.json()["datasets"]}
    assert "ds_ecommerce_demo" in ids

    detail = client.get("/api/datasets/ds_ecommerce_demo")
    assert detail.status_code == 200
    body = detail.json()
    assert any(m["id"] == "gmv" for m in body["metrics"])
    assert body["warehouse"].endswith("warehouse.duckdb")

    meta = client.get("/api/meta", params={"dataset_id": "ds_ecommerce_demo"})
    assert meta.status_code == 200
    assert meta.json()["dataset_id"] == "ds_ecommerce_demo"


def test_turn_resolves_session_dataset(client):
    sid = client.post("/api/sessions", json={}).json()["session_id"]
    resp = client.post(f"/api/sessions/{sid}/turns",
                       json={"message": "总销售额是多少？"})
    assert resp.status_code == 200
    assert "sql.executed" in resp.text
    state = client.get(f"/api/sessions/{sid}").json()
    assert state["dataset_id"] == "ds_ecommerce_demo"
    assert state["status"] == "DONE"
