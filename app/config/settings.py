"""Central configuration. All tunables live here (env-overridable)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv optional
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parents[2]

if load_dotenv:
    # project .env is authoritative for this app (overrides a shell-wide default)
    load_dotenv(BASE_DIR / ".env", override=True)

DATA_DIR = BASE_DIR / "data"

# One folder per raw dataset: the source workbook plus every artifact derived
# from it (warehouse, catalog, value dictionary, quality report).
DATASET = os.getenv("DATASET", "ecommerce_agent_demo")
DATASETS_DIR = DATA_DIR / "datasets"
DATASET_DIR = DATASETS_DIR / DATASET
RAW_DIR = DATASET_DIR / "raw"
WAREHOUSE_PATH = Path(os.getenv("WAREHOUSE_PATH", DATASET_DIR / "warehouse.duckdb"))
CATALOG_PATH = DATASET_DIR / "catalog.json"
DIM_VALUES_PATH = DATASET_DIR / "dim_values.json"
QUALITY_PATH = DATASET_DIR / "quality_report.json"

# Runtime state and developer diagnostics are dataset-independent.
RUNTIME_DIR = DATA_DIR / "runtime"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
LOG_DIR = RUNTIME_DIR / "logs"
TRACE_DIR = RUNTIME_DIR / "traces"
DEV_DIR = DATA_DIR / "dev"

SEMANTIC_PATH = BASE_DIR / "app" / "domain" / "semantic" / "semantic.yaml"
BINDING_PATH = BASE_DIR / "app" / "domain" / "semantic" / "binding.yaml"

# ---- LLM ----
LLM_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
LLM_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL = os.getenv("ANTHROPIC_MODEL", "deepseek-v4-flash[1m]")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
# When no API key is configured the agent falls back to the deterministic
# rule-based parser so the demo remains runnable offline.
LLM_ENABLED = bool(LLM_API_KEY) and os.getenv("LLM_DISABLED", "").lower() not in {"1", "true"}

# ---- Controller ----
MAX_STEPS = int(os.getenv("MAX_STEPS", "24"))
MAX_ROW_LIMIT = int(os.getenv("MAX_ROW_LIMIT", "2000"))
SQL_TIMEOUT_MS = int(os.getenv("SQL_TIMEOUT_MS", "8000"))
DEFAULT_MONTH_WINDOW = int(os.getenv("DEFAULT_MONTH_WINDOW", "12"))

# ---- Context / compaction ----
CONTEXT_TOKEN_BUDGET = int(os.getenv("CONTEXT_TOKEN_BUDGET", "12000"))
KEEP_RECENT_TURNS = int(os.getenv("KEEP_RECENT_TURNS", "4"))

# ---- Server ----
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
