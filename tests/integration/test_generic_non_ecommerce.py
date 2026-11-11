"""Non-e-commerce regression: can a brand-new domain go end to end?

Every dataset shipped with the repo is the same e-commerce workbook, so the
"generic" claim had no end-to-end proof. These tests ingest a SaaS
subscription / support-ticket domain and a Chinese-header workbook, then query
the generated warehouse through the normal plan -> compiler -> executor path.

The LLM is disabled by conftest, so this exercises exactly the deterministic
fallback: structure inference, heuristic drafts, relation inference.
"""
from __future__ import annotations

import json

from app.domain.pipeline import run_query
from app.domain.plan import QueryPlan
from app.domain.semantic.loader import load_semantic
from app.ingest.generic_pipeline import analyze_files


def _draft(root):
    return json.loads((root / "semantic_draft.json").read_text(encoding="utf-8"))


def _sem(root):
    return load_semantic(
        root / "semantic.yaml", root / "binding.yaml",
        catalog_path=root / "catalog.json",
    )


def _column_ref(root, label: str) -> tuple[str, str]:
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    for table_id, table in (catalog.get("tables") or {}).items():
        for column in table.get("columns") or []:
            if str(column.get("label")) == label:
                return table_id, str(column.get("name"))
    raise AssertionError(f"列不存在: {label}")


def _metric_for(root, table_id: str, column: str) -> str:
    needle = f"{table_id}.{column}"
    for metric in _draft(root)["metrics"]:
        if needle in str(metric.get("expr") or ""):
            return metric["id"]
    raise AssertionError(f"没有针对 {needle} 的指标")


def _dimension_for(root, table_id: str, column: str) -> str:
    needle = f"{table_id}.{column}"
    for dim in _draft(root)["dimensions"]:
        if needle in str(dim.get("column") or ""):
            return dim["id"]
    raise AssertionError(f"没有针对 {needle} 的维度")


def _query(root, metric, dimensions=None):
    sem = _sem(root)
    plan = QueryPlan(metric=metric, dimensions=dimensions or [], limit=100)
    return run_query(plan, sem, db_path=root / "warehouse.duckdb")


def test_saas_subscriptions_query_by_plan_and_joined_dimension(tmp_path):
    accounts = tmp_path / "accounts.csv"
    accounts.write_text(
        "account_id,account_name,industry,signup_date\n"
        "A1,Acme,fintech,2025-01-02\n"
        "A2,Beta,health,2025-01-05\n"
        "A3,Gamma,fintech,2025-02-01\n",
        encoding="utf-8",
    )
    subscriptions = tmp_path / "subscriptions.csv"
    subscriptions.write_text(
        "subscription_id,account_id,plan,start_date,seats,mrr\n"
        "S1,A1,Pro,2025-01-03,10,1000\n"
        "S2,A1,Pro,2025-02-03,10,1200\n"
        "S3,A2,Basic,2025-01-06,3,150\n"
        "S4,A3,Pro,2025-02-10,20,2000\n"
        "S5,A3,Basic,2025-03-01,5,250\n",
        encoding="utf-8",
    )

    root = tmp_path / "ds"
    out = analyze_files([accounts, subscriptions], root, "ds_saas_demo", "SaaS 订阅")

    draft = _draft(root)
    accepted = [rel for rel in draft["relations"] if rel["accepted"]]
    assert accepted, "subscriptions.account_id -> accounts.account_id 未被推断"
    assert len(out.tables) == 2

    mrr_table, mrr_column = _column_ref(root, "mrr")
    metric = _metric_for(root, mrr_table, mrr_column)

    # Own-table dimension: plan (Pro / Basic).
    plan_dim = _dimension_for(root, mrr_table, _column_ref(root, "plan")[1])
    outcome = _query(root, metric, dimensions=[plan_dim])
    assert outcome.ok, outcome.error
    assert {row[0]: round(row[1], 2) for row in outcome.result.rows} == {
        "Pro": 4200.0,
        "Basic": 400.0,
    }

    # Joined dimension: industry lives on accounts, 1:N -> N:1 join.
    industry_table, industry_column = _column_ref(root, "industry")
    industry_dim = _dimension_for(root, industry_table, industry_column)
    joined = _query(root, metric, dimensions=[industry_dim])
    assert joined.ok, joined.error
    assert {row[0]: round(row[1], 2) for row in joined.result.rows} == {
        "fintech": 4450.0,
        "health": 150.0,
    }


def test_chinese_headers_are_queryable_without_llm(tmp_path):
    source = tmp_path / "月度收入.csv"
    source.write_text(
        "账户编号,套餐,月份,收入\n"
        "A1,专业版,2025-01,1000\n"
        "A1,专业版,2025-02,1200\n"
        "A2,基础版,2025-01,150\n",
        encoding="utf-8",
    )
    root = tmp_path / "ds"
    analyze_files([source], root, "ds_cn_revenue", "中文月度收入")

    table_id, column = _column_ref(root, "收入")
    metric = _metric_for(root, table_id, column)
    outcome = _query(root, metric)
    assert outcome.ok, outcome.error
    assert round(outcome.result.rows[0][0], 2) == 2350.0

    plan_column = _column_ref(root, "套餐")[1]
    dim = _dimension_for(root, table_id, plan_column)
    grouped = _query(root, metric, dimensions=[dim])
    assert grouped.ok, grouped.error
    assert {row[0]: round(row[1], 2) for row in grouped.result.rows} == {
        "专业版": 2200.0,
        "基础版": 150.0,
    }
