"""Q01–Q07 through the real pipeline (semantic layer -> compiler -> DuckDB)."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain.pipeline import run_query  # noqa: E402
from app.domain.plan import (  # noqa: E402
    CompareSpec, HavingSpec, NestedSpec, OrderSpec, QueryPlan, TimeRange,
)
from app.domain.semantic.loader import load_semantic  # noqa: E402

FULL = TimeRange(start=date(2024, 1, 1), end=date(2025, 12, 26))
APR = TimeRange(start=date(2025, 4, 1), end=date(2025, 5, 1))
MAY = TimeRange(start=date(2025, 5, 1), end=date(2025, 6, 1))

PLANS = {
    "Q01": QueryPlan(metric="order_count", time=FULL, limit=1),
    "Q02": QueryPlan(metric="gmv", time=FULL, limit=1),
    "Q03": QueryPlan(metric="gmv", time=APR, compare=CompareSpec(type="prev_period"), limit=1),
    "Q04": QueryPlan(metric="gmv", dimensions=["category"], time=APR,
                     order_by=[OrderSpec(field="value", direction="desc")], limit=10),
    "Q05": QueryPlan(metric="delay_rate", dimensions=["seller"], time=MAY,
                     having=[HavingSpec(metric="order_count", op="gte", value=10)],
                     ratio_grain="order",
                     order_by=[OrderSpec(field="value", direction="desc")], limit=10),
    "Q06": QueryPlan(metric="avg_review_score", dimensions=["late"], time=FULL, limit=10),
    "Q07": QueryPlan(metric="repeat_customer_count", time=FULL,
                     nested=NestedSpec(min_orders=2), limit=1),
}


def main() -> int:
    sem = load_semantic()
    ok = True
    out = {}
    for qid, plan in PLANS.items():
        res = run_query(plan, sem)
        out[qid] = {
            "ok": res.ok,
            "rows": res.result.rows if res.result else None,
            "checks": [c.as_dict() for c in res.checks],
            "error": res.error,
            "sql": res.compiled.sql if res.compiled else None,
        }
        print(f"--- {qid} ok={res.ok} rows={res.result.rows if res.result else None}")
        if res.error:
            print("   error:", res.error)
        for c in res.checks:
            print(f"   [{c.severity}] {c.title}: {c.detail}")

    r = out
    if r["Q01"]["rows"][0][0] != 10714:
        ok = False
    if abs(r["Q02"]["rows"][0][0] - 4858920.84) > 0.01:
        ok = False
    if abs(r["Q03"]["rows"][0][3] - 0.15) > 0.0001:
        ok = False
    if r["Q04"]["rows"][0][0] != "数码家电" or abs(r["Q04"]["rows"][0][1] - 105245.50) > 0.01:
        ok = False
    if r["Q05"]["rows"][0][0] != "广州美妆个护商家004" or abs(r["Q05"]["rows"][0][3] - 0.9153) > 0.0001:
        ok = False
    scores = {row[0]: row[1] for row in r["Q06"]["rows"]}
    if abs(scores.get("延迟", 0) - 2.00) > 0.01 or abs(scores.get("准时", 0) - 4.02) > 0.01:
        ok = False
    if r["Q07"]["rows"][0][0] != 2378:
        ok = False

    Path("tests/fixtures/plan_goldens.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("PLAN GOLDENS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
