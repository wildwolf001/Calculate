from __future__ import annotations

import json

from app.datasets.index import build_index, validate_index
from app.domain.semantic.loader import Dimension, Metric, SemanticLayer
from app.llm.parse_intent import LLMIntentParser
from app.llm.semantic_brief import build_brief


def _sem():
    return SemanticLayer(
        version=3,
        dataset="refund_demo",
        tables={
            "t1": {
                "grain": ["c01"],
                "time": "c02",
                "columns": ["c01", "c02", "c03", "c04"],
            },
            "t2": {"grain": ["c01"], "time": None, "columns": ["c01", "c02"]},
        },
        joins={"t1": {"t2": {"left": "c03", "right": "c01", "card": "N:1"}}},
        pre_aggregates={},
        metrics={
            "m_orders": Metric("m_orders", {
                "name_zh": "成交单数",
                "type": "aggregate",
                "fact": "t1",
                "expr": "COUNT(DISTINCT t1.c01)",
                "time_via": "t1.c02",
                "unit": "单",
                "supported_grain": ["none", "d_channel"],
                "provenance": "documented",
            }),
            "m_refunds": Metric("m_refunds", {
                "name_zh": "退单数量",
                "type": "aggregate",
                "fact": "t1",
                "expr": "SUM(t1.c04)",
                "time_via": "t1.c02",
                "unit": "单",
                "supported_grain": ["none", "d_channel"],
                "provenance": "inferred",
            }),
        },
        dimensions={
            "d_channel": Dimension("d_channel", {
                "name_zh": "渠道",
                "kind": "categorical",
                "column": "t1.c03",
                "synonyms": ["渠道", "来源"],
            }),
            "d_late": Dimension("d_late", {
                "name_zh": "是否延迟",
                "kind": "derived",
                "expr": "t1.c04 > 0",
                "labels": {"true": "延迟", "false": "准时"},
                "requires": "t1",
            }),
        },
        policies={},
        time_range=("2025-01-01", "2025-12-31"),
        binding={"tables": {}},
        raw={},
    )


def _catalog():
    return {
        "tables": {
            "t1": {
                "rows": 12,
                "columns": [
                    {"name": "c01", "type": "VARCHAR", "distinct": 12, "null_rate": 0.0},
                    {"name": "c02", "type": "DATE", "distinct": 6, "null_rate": 0.0},
                    {"name": "c03", "type": "VARCHAR", "distinct": 2, "null_rate": 0.0,
                     "samples": ["线上", "线下"]},
                    {"name": "c04", "type": "INTEGER", "distinct": 3, "null_rate": 0.0},
                ],
            }
        }
    }


def test_index_contains_expressions_and_values():
    index = build_index(
        dataset_id="ds_refund",
        data_version="sha256:abc",
        semantic_version=3,
        sem=_sem(),
        catalog=_catalog(),
        dim_values={"values": {"t1.c03": ["线上", "线下"]}},
        documentation={"rules": [{"name": "退单率", "definition": "退单/成交",
                                   "formula": "退单数量 / 成交单数"}]},
        capabilities={"m_orders": {"allowed_dimensions": ["none", "d_channel"]}},
    )
    assert index["version"] == 1
    assert index["metrics"]["m_orders"]["expr"] == "COUNT(DISTINCT t1.c01)"
    assert index["metrics"]["m_refunds"]["provenance"] == "inferred"
    assert index["dimensions"]["d_late"]["labels"]["true"] == "延迟"
    assert index["dimensions"]["d_channel"]["values"] == ["线上", "线下"]
    assert index["relations"][0]["card"] == "N:1"
    assert index["docs"][0]["formula"] == "退单数量 / 成交单数"
    assert validate_index(index) == []


def test_index_is_deterministic():
    kwargs = dict(
        dataset_id="ds_refund",
        data_version="sha256:abc",
        semantic_version=3,
        sem=_sem(),
        catalog=_catalog(),
        dim_values={"values": {"t1.c03": ["线上", "线下"]}},
    )
    first = json.dumps(build_index(**kwargs), ensure_ascii=False, sort_keys=True)
    second = json.dumps(build_index(**kwargs), ensure_ascii=False, sort_keys=True)
    assert first == second


def test_brief_contains_relevant_metric_and_is_bounded():
    index = build_index(
        dataset_id="ds_refund",
        data_version="sha256:abc",
        semantic_version=3,
        sem=_sem(),
        catalog=_catalog(),
        dim_values={"values": {"t1.c03": ["线上", "线下"]}},
    )
    brief = build_brief(index, "退单率是多少", max_chars=4000)
    assert "m_refunds" in brief
    assert "SUM(t1.c04)" in brief
    assert len(brief) <= 4000


def test_brief_retrieves_chinese_name_over_alphabetical_order():
    index = {
        "dataset_id": "d",
        "semantic_version": 1,
        "metrics": {
            **{
                f"m{i:02d}": {
                    "name": f"指标{i}",
                    "type": "aggregate",
                    "expr": "COUNT(*)",
                    "fact": "t1",
                    "supported_dimensions": ["d_seller"],
                }
                for i in range(10)
            },
            "m_llm_delay": {
                "name": "延迟率",
                "type": "ratio",
                "expr": "late / delivered",
                "fact": "t1",
                "supported_dimensions": ["d_seller"],
            },
        },
        "dimensions": {
            "d_seller": {"name": "商家名称", "kind": "categorical", "synonyms": ["商家"]},
        },
        "tables": {},
    }
    brief = build_brief(index, "5月已送达订单不少于10笔的商家中，延迟率最高的是谁？")
    assert "m_llm_delay" in brief
    assert "商家名称" in brief


def test_parser_can_use_brief(tmp_path):
    dim_values = tmp_path / "dim_values.json"
    dim_values.write_text('{"values": {}, "aliases": {}, "id_maps": {}}', encoding="utf-8")
    index = build_index(
        dataset_id="ds_refund",
        data_version="sha256:abc",
        semantic_version=3,
        sem=_sem(),
        catalog=_catalog(),
        dim_values={"values": {"t1.c03": ["线上", "线下"]}},
    )
    parser = LLMIntentParser(
        _sem(), client=None, dim_values_path=dim_values,
        index=index, use_brief=True,
    )
    system = parser._system_for("退单率是多少")
    assert "m_refunds" in system
    assert "SUM(t1.c04)" in system
