"""Runtime model selection must persist keys without returning them to the UI."""
from __future__ import annotations

from app.llm import runtime_config
from app.llm.client import LLMClient


def test_qwen_config_roundtrip_and_masked_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "llm_config.json")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    public = runtime_config.save_config({
        "provider": "qwen",
        "model": "qwen3.8-max-0902",
        "base_url": "https://example.test/compatible-mode/v1",
        "api_key": "sk-qwen-secret-1234",
    })
    assert public["provider"] == "qwen"
    assert public["protocol"] == "openai"
    assert public["has_api_key"] is True
    assert public["api_key_masked"].endswith("1234")
    assert "sk-qwen-secret" not in str(public)

    secret = runtime_config.load_config()
    assert secret["api_key"] == "sk-qwen-secret-1234"
    assert secret["model"] == "qwen3.8-max-0902"


def test_switching_provider_does_not_reuse_previous_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "llm_config.json")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    runtime_config.save_config({
        "provider": "deepseek",
        "model": "deepseek-v4-flash[1m]",
        "base_url": "https://api.deepseek.com/anthropic",
        "api_key": "deepseek-key",
    })
    public = runtime_config.save_config({
        "provider": "qwen",
        "model": "qwen3.8-max-0902",
        "base_url": "https://example.test/compatible-mode/v1",
    })
    assert public["provider"] == "qwen"
    assert public["has_api_key"] is False


def test_llm_client_provider_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "CONFIG_PATH", tmp_path / "llm_config.json")
    client = LLMClient(api_key="x", provider="qwen", enabled=True)
    assert client.provider == "qwen"
    assert client.available is True
    assert LLMClient(api_key="", provider="qwen").available is False


def test_openai_compatible_qwen_call(monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.called = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            self.called = (url, kwargs)
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    client = LLMClient(
        api_key="qwen-key",
        base_url="https://qwen.example/v1",
        model="qwen3.8-max-0902",
        provider="qwen",
        enabled=True,
    )
    assert client.complete_text("system", "user", max_tokens=20) == "OK"
