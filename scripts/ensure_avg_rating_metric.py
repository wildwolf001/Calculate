"""Publish a published AVG(rating) metric for a dataset.

Why: a review-rating question currently depends on the parser inventing a
derived metric at chat time. That is fragile (wrong column, wrong time axis).
This script freezes the metric into the semantic layer via the normal publish
path, so the parser can bind to it like any other published metric.

Usage:
    python scripts/ensure_avg_rating_metric.py --dataset ds_11670cbd5274
    python scripts/ensure_avg_rating_metric.py --dataset ds_11670cbd5274 --publish

The script backs up semantic.yaml / semantic_draft.json next to the dataset
before writing, and refuses to publish if the metric cannot be executed.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.datasets.publish import publish_dataset  # noqa: E402
from app.datasets.registry import DatasetRegistry  # noqa: E402


def add_metric(dataset_dir: Path, *,
               metric_id: str = "m_llm_平均评分",
               name: str = "平均评分",
               fact: str = "t_006",
               operand: str = "t_006.c03",
               scope: dict | None = None,
               time_via: str | None = "t_006.c06",
               unit: str | None = "分",
               definition: str = "已送达且有评价订单的评价分平均值（1-5 分）。",
               synonyms: list[str] | None = None) -> dict:
    semantic_path = dataset_dir / "semantic.yaml"
    draft_path = dataset_dir / "semantic_draft.json"
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    if semantic_path.exists():
        shutil.copy2(semantic_path, dataset_dir / f"semantic.yaml.before-avg-{stamp}")
    if draft_path.exists():
        shutil.copy2(draft_path, dataset_dir / f"semantic_draft.json.before-avg-{stamp}")

    semantic = yaml.safe_load(semantic_path.read_text(encoding="utf-8")) or {}
    metrics = semantic.setdefault("metrics", {})
    metric = {
        "name_zh": name,
        "type": "aggregate",
        "fact": fact,
        "expr": f"AVG({operand})",
        "scope": scope if scope is not None else {"t_003.c03": "delivered"},
        "time_via": time_via,
        "unit": unit,
        "definition": definition,
        "synonyms": synonyms or ["评分", "平均分", name, "评价分数", "评分均值"],
        "provenance": "human",
        "selected": True,
        "confidence": 0.9,
    }
    metrics[metric_id] = metric
    semantic_path.write_text(
        yaml.safe_dump(semantic, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    if draft_path.exists():
        try:
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            draft = {}
        items = [item for item in draft.get("metrics", []) if item.get("id") != metric_id]
        items.append({
            "id": metric_id,
            "name_zh": name,
            "name": name,
            "aggregation": "AVG",
            "fact": fact,
            "expr": metric["expr"],
            "scope": metric["scope"],
            "time_via": time_via,
            "synonyms": metric["synonyms"],
            "unit": unit,
            "definition": definition,
            "confidence": 0.9,
            "selected": True,
            "provenance": "human",
        })
        draft["metrics"] = items
        draft_path.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return metric


def main() -> int:
    parser = argparse.ArgumentParser(description="Add/publish an AVG rating metric")
    parser.add_argument("--dataset", default="ds_11670cbd5274")
    parser.add_argument("--datasets-dir", default=str(ROOT / "data" / "datasets"))
    parser.add_argument("--metric-id", default="m_llm_平均评分")
    parser.add_argument("--fact", default="t_006")
    parser.add_argument("--operand", default="t_006.c03")
    parser.add_argument("--time-via", default="t_006.c06")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()

    dataset_dir = Path(args.datasets_dir) / args.dataset
    if not (dataset_dir / "semantic.yaml").exists():
        print(f"dataset not found: {dataset_dir}", file=sys.stderr)
        return 2

    metric = add_metric(
        dataset_dir, metric_id=args.metric_id, fact=args.fact,
        operand=args.operand, time_via=args.time_via or None,
    )
    print(f"metric written: {args.metric_id} = {metric['expr']}")
    if not args.publish:
        print("skip publish (pass --publish to publish all selected metrics)")
        return 0

    registry = DatasetRegistry(root=Path(args.datasets_dir))
    semantic = yaml.safe_load((dataset_dir / "semantic.yaml").read_text(encoding="utf-8"))
    result = publish_dataset(
        registry, args.dataset,
        selected_metrics=list((semantic.get("metrics") or {})),
        selected_dimensions=list((semantic.get("dimensions") or {})),
    )
    print(json.dumps(
        {k: result.get(k) for k in ("dataset_id", "status", "semantic_version")},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
