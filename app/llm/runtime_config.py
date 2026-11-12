"""Runtime-selectable LLM configuration.

The server defaults to the existing environment, but the UI can switch between
the DeepSeek (Anthropic-compatible) and Qwen (OpenAI-compatible) gateways
without editing `.env` and restarting. The API key is persisted server-side and
is only ever returned masked to the browser.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.settings import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    RUNTIME_DIR,
)

_LLM_DISABLED = os.getenv("LLM_DISABLED", "").lower() in {"1", "true", "yes"}

CONFIG_PATH = Path(os.getenv("LLM_CONFIG_PATH", RUNTIME_DIR / "llm_config.json"))

PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "protocol": "anthropic",
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-flash[1m]",
    },
    "qwen": {
        "label": "Qwen",
        "protocol": "openai",
        "base_url": "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        "model": "qwen3.8-max-0902",
    },
}

_lock = threading.Lock()


def _detect_provider(model: str, base_url: str) -> str:
    text = f"{model} {base_url}".lower()
    if "qwen" in text or "dashscope" in text or "qianwen" in text:
        return "qwen"
    return "deepseek"


def _env_config() -> dict[str, Any]:
    provider = _detect_provider(LLM_MODEL, LLM_BASE_URL)
    preset = PRESETS[provider]
    return {
        "provider": provider,
        "model": LLM_MODEL or preset["model"],
        "base_url": LLM_BASE_URL or preset["base_url"],
        "protocol": "openai" if provider == "qwen" else preset["protocol"],
        "api_key": LLM_API_KEY,
        "source": "env",
    }


def _read_file() -> dict[str, Any] | None:
    try:
        if not CONFIG_PATH.exists():
            return None
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_config() -> dict[str, Any]:
    """Return the effective config, including the secret key."""
    base = _env_config()
    saved = _read_file()
    saved_key = bool(saved and "api_key" in saved)
    if saved:
        provider = str(saved.get("provider") or base["provider"])
        provider = provider if provider in PRESETS else base["provider"]
        preset = PRESETS[provider]
        base.update({
            "provider": provider,
            "model": str(saved.get("model") or preset["model"]),
            "base_url": str(saved.get("base_url") or preset["base_url"]),
            "protocol": preset["protocol"],
            "source": "runtime",
        })
        if saved_key:
            base["api_key"] = str(saved.get("api_key") or "")
    # Qwen commonly uses a provider-specific env variable.
    if not saved_key and not base.get("api_key") and base["provider"] == "qwen":
        base["api_key"] = os.getenv("DASHSCOPE_API_KEY", "")
    if not saved_key and not base.get("api_key") and base["provider"] == "deepseek":
        base["api_key"] = os.getenv("DEEPSEEK_API_KEY", "")
    base["enabled"] = bool(base.get("api_key")) and not _LLM_DISABLED
    return base


def _mask(key: str) -> str:
    if not key:
        return ""
    return "••••••••" + key[-4:]


def public_config() -> dict[str, Any]:
    cfg = load_config()
    provider = cfg["provider"]
    preset = PRESETS[provider]
    return {
        "provider": provider,
        "protocol": cfg["protocol"],
        "label": preset["label"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "enabled": cfg["enabled"],
        "has_api_key": bool(cfg.get("api_key")),
        "api_key_masked": _mask(cfg.get("api_key", "")),
        "source": cfg.get("source", "env"),
        "presets": PRESETS,
    }


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("配置必须是 JSON 对象")
    current = load_config()
    provider = str(payload.get("provider") or current["provider"]).strip().lower()
    if provider not in PRESETS:
        raise ValueError(f"未知模型提供方: {provider}")
    preset = PRESETS[provider]
    model = str(payload.get("model") or preset["model"]).strip()
    base_url = str(payload.get("base_url") or preset["base_url"]).strip()
    if not model:
        raise ValueError("模型名不能为空")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")

    existing_key = current.get("api_key", "")
    if payload.get("clear_api_key"):
        api_key = ""
    elif isinstance(payload.get("api_key"), str) and payload["api_key"].strip():
        api_key = payload["api_key"].strip()
    elif provider != current.get("provider"):
        # Never reuse one provider's key for another provider.
        api_key = ""
    else:
        api_key = existing_key

    data = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, CONFIG_PATH)
    return public_config()
