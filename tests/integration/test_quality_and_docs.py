"""M5 quality fixes + documentation-as-authoritative-semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.core.event_store import EventStore
from app.datasets.context import load_context
from app.datasets.manifest import compute_data_version
from app.datasets.publish import publish_dataset
from app.datasets.registry import DatasetError, DatasetRegistry
from app.factory import build_service
from app.domain.plan import QueryPlan
from app.domain.pipeline import run_query
from app.domain.semantic.loader import load_semantic, normalize_date_bound
from app.ingest.documentation import apply_documentation, parse_document
from app.ingest.generic_pipeline import analyze_files
from app.ingest.generic_types import IngestOptions
from app.llm.rule_parser import RuleParser


def _write_store(root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "orders.csv": (
            "order_id,customer_id,order_status,purchase_at,delivered_at,estimated_delivery_at\n"
            "O1,C1,delivered,2025-01-01 10:00:00,2025-01-05 10:00:00,2025-01-04 10:00:00\n"
            "O2,C2,delivered,2025-01-02 10:00:00,2025-01-06 10:00:00,2025-01-08 10:00:00\n"
            "O3,C1,cancelled,2025-01-03 10:00:00,,2025-01-09 10:00:00\n"
            "O4,C1,delivered,2025-01-04 10:00:00,2025-01-07 10:00:00,2025-01-09 10:00:00\n"
        ),
        "order_items.csv": (
            "order_id,order_item_id,product_id,seller_id,price,freight_value\n"
            "O1,1,P1,S1,100,10\n"
            "O1,2,P2,S1,50,5\n"
            "O2,1,P1,S2,200,20\n"
            "O3,1,P1,S1,70,7\n"
            "O4,1,P2,S2,25,2\n"
        ),
        "order_payments.csv": (
            "order_id,payment_sequence,payment_type,payment_value\n"
            "O1,1,credit_card,100\n"
            "O2,1,credit_card,200\n"
            "O2,2,voucher,50\n"
            "O3,1,voucher,70\n"
            "O4,1,credit_card,25\n"
        ),
        "order_reviews.csv": (
            "review_id,order_id,review_score,review_title\n"
            "R1,O1,1,差评\n"
            "R2,O2,4,好评\n"
            "R3,O4,5,好评\n"
        ),
        "customers.csv": (
            "customer_id,customer_unique_id,customer_city,customer_state,email\n"
            "C1,U1,北京,北京,c1@example.com\n"
            "C2,U2,上海,上海,c2@example.com\n"
        ),
        "products.csv": (
            "product_id,product_name,category_id\n"
            "P1,商品A,electronics\n"
            "P2,商品B,home\n"
        ),
        "sellers.csv": "seller_id,seller_name\nS1,商家甲\nS2,商家乙\n",
        "categories.csv": "category_id,category_name_zh\nhome,家居生活\nelectronics,数码家电\n",
        "practice_questions.csv": (
            "question_id,level,question,expected_answer\n"
            "Q1,基础,有多少订单,4\n"
            "合计,,,\n"
        ),
    }
    paths = []
    for name, text in files.items():
        path = root / name
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return paths


def _ctx(root: Path):
    return load_context(
        dataset_id="ds_quality_docs",
        name="质量与文档测试",
        root=root,
        warehouse=root / "warehouse.duckdb",
        semantic_path=root / "semantic.yaml",
        binding_path=root / "binding.yaml",
        mode="draft",
        data_version=compute_data_version(root / "warehouse.duckdb"),
        catalog_path=root / "catalog.json",
    )


def _metric_by_name(sem, name: str) -> str:
    for metric in sem.metrics.values():
        if metric.name == name:
            return metric.id
    raise AssertionError(f"metric not found: {name}")


def test_time_bounds_are_date_only():
    assert normalize_date_bound("2025-01-01T11:20:00") == "2025-01-01"


def test_quality_pii_facts_and_key_heuristics(tmp_path):
    files = _write_store(tmp_path / "raw")
    root = tmp_path / "ds"
    out = analyze_files(files, root, "ds_quality_docs", "质量与文档测试")
    labels = {t.label: t for t in out.tables}
    assert "practice_questions" not in labels  # suspected documentation skipped
    assert labels["order_payments"].grain == ["c01", "c02"]
    assert labels["customers"].role == "dimension"
    assert labels["products"].role == "dimension"
    assert labels["sellers"].role == "dimension"

    draft = json.loads((root / "semantic_draft.json").read_text(encoding="utf-8"))
    table_by_id = {t["table_id"]: t["label"] for t in draft["tables"]}
    fact_labels = {table_by_id[tid] for tid in draft["facts"]}
    assert {"order_items", "orders"} <= fact_labels
    assert not ({"customers", "products", "sellers", "categories"} & fact_labels)
    names = {m["name_zh"] for m in draft["metrics"] if m.get("selected")}
    assert "商品销售额" in names
    assert "商品数" in names
    assert "订单数" in names
    assert all("zip_code" not in n for n in names)

    pii = {
        c["label"]
        for t in draft["tables"]
        for c in t["columns"] if c.get("is_pii")
    }
    assert "email" in pii
    assert "product_name" not in pii
    assert "customer_name" not in pii
    assert "seller_name" not in pii


def test_totals_policy_is_visible_and_configurable(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "sales.csv"
    source.write_text(
        "day,amount\n2025-01-01,10\n2025-01-02,20\n合计,30\n",
        encoding="utf-8",
    )
    keep_root = tmp_path / "keep"
    out_keep = analyze_files(
        [source], keep_root, "ds_keep", "keep",
        options=IngestOptions(totals_policy="keep"),
    )
    assert out_keep.tables[0].rows == 3
    quality_keep = json.loads(
        (keep_root / "quality_report.json").read_text(encoding="utf-8")
    )
    totals_keep = next(c for c in quality_keep["checks"] if c["id"] == "totals_rows")
    assert totals_keep["severity"] == "warn"
    assert totals_keep["count"] == 1

    drop_root = tmp_path / "drop"
    out_drop = analyze_files(
        [source], drop_root, "ds_drop", "drop",
        options=IngestOptions(totals_policy="exclude"),
    )
    assert out_drop.tables[0].rows == 2


def test_generic_parser_maps_business_questions(tmp_path):
    files = _write_store(tmp_path / "raw")
    root = tmp_path / "ds"
    analyze_files(files, root, "ds_quality_docs", "质量与文档测试")
    sem = load_semantic(root / "semantic.yaml", root / "binding.yaml",
                        catalog_path=root / "catalog.json")
    parser = RuleParser(sem, root / "dim_values.json")
    for question, expected in [
        ("总销售额是多少", "商品销售额"),
        ("一共有几种商品", "商品数"),
        ("订单数是多少", "订单数"),
    ]:
        result = parser.parse(question, {})
        assert result.slots.get("metric")
        assert sem.metric(result.slots["metric"]).name == expected

    result = parser.parse("按客户城市统计销售额", {})
    assert result.slots.get("metric")
    assert result.slots.get("dimensions")
    dim = sem.dimension(result.slots["dimensions"][0])
    assert dim.raw.get("name_zh") == "客户城市"


def test_readme_documentation_becomes_authoritative(tmp_path):
    files = _write_store(tmp_path / "raw")
    root = tmp_path / "ds"
    analyze_files(files, root, "ds_quality_docs", "质量与文档测试")
    ctx = _ctx(root)
    readme = (
        "# 电商数据\n"
        "- 数据范围：2025-01 至 2025-12\n"
        "- 销售额口径：已送达订单的 order_items.price，不含运费\n"
        "- 订单数：按 order_id 去重\n"
        "- 延迟订单：delivered_at > estimated_delivery_at\n"
        "- 低分评价：review_score <= 2\n"
    )
    doc = parse_document(readme, "README.md", ctx)
    names = {rule["name"] for rule in doc["rules"]}
    assert {"销售额", "订单数", "延迟订单", "低分评价"} <= names
    # LLM-assisted extraction sometimes duplicates the formula as a raw filter
    # value. It must not become an equality scope such as review_score='<= 2'.
    low_rule = next(rule for rule in doc["rules"] if rule["name"] == "低分评价")
    low_rule["filters"]["review_score"] = "<= 2"

    result = apply_documentation(ctx, doc)
    assert result["applied"]
    sem = load_semantic(root / "semantic.yaml", root / "binding.yaml",
                        catalog_path=root / "catalog.json")
    sales = _metric_by_name(sem, "销售额")
    late = _metric_by_name(sem, "延迟订单")
    low = _metric_by_name(sem, "低分评价")
    assert not any(
        value == "<= 2"
        for value in sem.metric(low).raw.get("scope", {}).values()
    )
    sales_out = run_query(QueryPlan(metric=sales, dimensions=[], limit=5), sem,
                          db_path=root / "warehouse.duckdb")
    late_out = run_query(QueryPlan(metric=late, dimensions=[], limit=5), sem,
                         db_path=root / "warehouse.duckdb")
    low_out = run_query(QueryPlan(metric=low, dimensions=[], limit=5), sem,
                        db_path=root / "warehouse.duckdb")
    assert sales_out.result.rows[0][0] == 375  # delivered items: 100+50+200+25
    assert late_out.result.rows[0][0] == 1
    assert low_out.result.rows[0][0] == 1


def test_count_vs_lookup_and_generic_member_list(tmp_path):
    files = _write_store(tmp_path / "raw")
    root = tmp_path / "ds"
    analyze_files(files, root, "ds_quality_docs", "质量与文档测试")
    sem = load_semantic(root / "semantic.yaml", root / "binding.yaml",
                        catalog_path=root / "catalog.json")
    parser = RuleParser(sem, root / "dim_values.json")

    count = parser.parse("一共有多少种品类", {})
    assert count.intent_type == "query"
    assert sem.metric(count.slots["metric"]).name == "品类数"
    out = run_query(QueryPlan(metric=count.slots["metric"], dimensions=[], limit=5),
                    sem, db_path=root / "warehouse.duckdb")
    assert out.result.rows[0][0] == 2

    listing = parser.parse("有哪些品类", {})
    assert listing.intent_type == "lookup"
    plan = QueryPlan(kind="member_list", list_dim=listing.lookup_dim, limit=100)
    out = run_query(plan, sem, db_path=root / "warehouse.duckdb")
    assert out.ok, out.error
    assert out.result.columns == ["value"]
    assert {row[0] for row in out.result.rows} == {"家居生活", "数码家电"}


def test_readiness_and_publish_gate(tmp_path):
    files = _write_store(tmp_path / "raw")
    root = tmp_path / "datasets"
    dataset_dir = root / "ds_quality_docs"
    analyze_files(files, dataset_dir, "ds_quality_docs", "质量与文档测试")
    readiness = json.loads((dataset_dir / "readiness.json").read_text(encoding="utf-8"))
    assert readiness["status"] == "ready"
    assert readiness["probes"] > 0

    registry = DatasetRegistry(root=root, default_id="ds_quality_docs")
    result = publish_dataset(registry, "ds_quality_docs")
    assert result["status"] == "published"
    ctx = registry.get("ds_quality_docs")
    assert ctx.mode == "published"
    readiness = json.loads((dataset_dir / "readiness.json").read_text(encoding="utf-8"))
    assert readiness["status"] == "ready"


def test_empty_project_service_refuses_sessions(tmp_path):
    registry = DatasetRegistry(root=tmp_path / "empty", default_id="ds_missing")
    assert registry.list() == []
    assert registry.default_id == ""
    with pytest.raises(DatasetError):
        registry.get()
    service = build_service(store=EventStore(tmp_path / "sessions"), registry=registry)
    import asyncio
    with pytest.raises(DatasetError):
        asyncio.run(service.create())


def test_metrics_document_ratio_and_repeat_customers(tmp_path):
    files = _write_store(tmp_path / "raw")
    root = tmp_path / "ds"
    analyze_files(files, root, "ds_quality_docs", "质量与文档测试")
    ctx = _ctx(root)
    doc_text = (
        "# 指标口径\n"
        "- 延迟率：延迟的已送达订单数 / 已送达订单数。\n"
        "- 复购客户：同一 customer_unique_id 完成至少 2 笔已送达订单。\n"
    )
    doc = parse_document(doc_text, "metrics.md", ctx)
    apply_documentation(ctx, doc)
    sem = load_semantic(root / "semantic.yaml", root / "binding.yaml",
                        catalog_path=root / "catalog.json")
    rate = _metric_by_name(sem, "延迟率")
    repeat = _metric_by_name(sem, "复购客户")
    rate_out = run_query(QueryPlan(metric=rate, dimensions=[], limit=5), sem,
                         db_path=root / "warehouse.duckdb")
    repeat_out = run_query(QueryPlan(metric=repeat, dimensions=[], limit=5), sem,
                           db_path=root / "warehouse.duckdb")
    assert abs(rate_out.result.rows[0][2] - 1 / 3) < 1e-6
    assert repeat_out.result.rows[0][0] == 1  # C1 has O1 + O4 delivered
