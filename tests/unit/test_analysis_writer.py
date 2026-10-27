"""LLM commentary may only repeat numbers present in deterministic section facts."""
from __future__ import annotations

from app.llm.analyze import LLMAnalysisWriter


CONTEXT = {
    "window": {},
    "focus": {},
    "sections": [{
        "id": "an1",
        "section": "规模与趋势",
        "title": "销售额趋势",
        "metric": "sales",
        "columns": ["month", "value"],
        "rows": [["2025-03", 100.0], ["2025-04", 150.0]],
        "insights": ["从 100 增至 150"],
        "caveats": [],
    }],
}


class FakeClient:
    def __init__(self, data, available=True):
        self.data = data
        self.available = available
        self.calls = 0

    def complete_json(self, system, user, max_tokens=0, stage=""):
        self.calls += 1
        return self.data


def test_grounded_commentary_is_kept_and_ungrounded_is_dropped():
    client = FakeClient({
        "core_conclusions": ["2025-04 为 150，高于 2025-03 的 100"],
        "sections": [
            {"id": "an1", "commentary": "从 100 增至 150。"},
            {"id": "an9", "commentary": "另一个数字为 999。"},
        ],
        "watchouts": ["注意样本量"],
    })
    result = LLMAnalysisWriter(client=client).write(CONTEXT)
    assert result is not None
    assert "an1" in result["sections"]
    assert "an9" not in result["sections"]
    assert result["core_conclusions"] == ["2025-04 为 150，高于 2025-03 的 100"]


def test_all_ungrounded_commentary_falls_back_to_template():
    client = FakeClient({
        "core_conclusions": ["增长 12345 元"],
        "sections": [{"id": "an1", "commentary": "增长 12345 元"}],
    })
    assert LLMAnalysisWriter(client=client).write(CONTEXT) is None


def test_unavailable_model_is_not_called():
    client = FakeClient({}, available=False)
    assert LLMAnalysisWriter(client=client).write(CONTEXT) is None
    assert client.calls == 0
