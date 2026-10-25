from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Deterministic, offline test run: never hit the real model.
os.environ["LLM_DISABLED"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.semantic.loader import load_semantic  # noqa: E402


@pytest.fixture(scope="session")
def sem():
    return load_semantic()
