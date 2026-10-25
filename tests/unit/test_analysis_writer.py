"""The analysis writer may only use numbers present in the section facts."""
from __future__ import annotations

from app.llm.analyze import LLMAnalysisWriter

CTX = {
    "window": {},
    "focus": {},
    "sections": [{
        "id": "an1",
        "section": "规模与趋势",
        "title": "近 12 个月商品销售额趋势",
        "metric": "gmv",
        "metric_name": "商品销售额",
        "unit": "元",
        "columns": ["month", "value"],
        "rows": [["2025-03", 100.0], ["2025-04", 150.0]],
        "row_count": 2,
        "insights": ["从 100 增至 150"],
        "caveats": [],
        "scope": "2025-03~2025-04",
        "result_id": "r1-an1",
    }],
}


class FakeClient:
    def __init__(self, data, available=True):
        self.data = data
        self.available = available
        self.calls = 0

    def complete_json(self, system, user, max_tokens=2400, stage="analyze"):
        self.calls += 1
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def test_grounded_commentary_is_kept_and_ungrounded_is_dropped():
    client = FakeClient({
        "core_conclusions": ["2025-04 为 150.0，高于 2025-03 的 100.0"],
        "sections": [
            {"id": "an1", "commentary": "从 100.0 增至 150.0。"},
            {"id": "an9", "commentary": "复购客户数为 999 人。"},
        ],
        "watchouts": ["注意样本量"],
    })
    out = LLMAnalysisWriter(client=client).write(CTX)
    assert out is not None
    assert "an1" in out["sections"]
    assert "an9" not in out["sections"]  # 999 is not in the provided facts
    assert len(out["core_conclusions"]) == 1
    assert out["watchouts"] == ["注意样本量"]


def test_all_ungrounded_returns_none(sem):
    client = FakeClient({
        "core_conclusions": ["增长 12345 元"],
        "sections": [{"id": "an1", "commentary": "增长 12345 元"}],
    })
    assert LLMAnalysisWriter(client=client).write(CTX) is None


def test_unavailable_client_returns_none():
    client = FakeClient({}, available=False)
    assert LLMAnalysisWriter(client=client).write(CTX) is None
    assert client.calls == 0
