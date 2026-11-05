from __future__ import annotations

from app.ingest.documentation import extract_policy


def test_extract_policy_from_readme_notes():
    policy = extract_policy([
        "数据范围：2025-01 至 2025-06",
        "订单数：180",
        "数据结构：参考 Olist 巴西电商公开数据集",
        "随机种子：20260921，可重复生成",
    ])
    assert policy["time_range"] == ["2025-01-01", "2025-06-30"]
    assert policy["expected_rows"] == 180
    assert policy["seed"] == 20260921
