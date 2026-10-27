from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Deterministic, offline test run: never hit the real model.
os.environ["LLM_DISABLED"] = "1"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import DATASET_DIR  # noqa: E402
from app.domain.semantic.loader import load_semantic  # noqa: E402

# A clean checkout has no bundled business dataset. Legacy e-commerce tests
# only run after that dataset has been imported into data/datasets.
DATA_AVAILABLE = (
    (DATASET_DIR / "dataset.json").exists()
    and (DATASET_DIR / "warehouse.duckdb").exists()
    and (DATASET_DIR / "semantic.yaml").exists()
)


@pytest.fixture(scope="session")
def sem():
    if not DATA_AVAILABLE:
        pytest.skip("未导入默认数据源，跳过依赖内置数据集的测试")
    return load_semantic(
        DATASET_DIR / "semantic.yaml",
        DATASET_DIR / "binding.yaml",
        catalog_path=DATASET_DIR / "catalog.json",
    )
