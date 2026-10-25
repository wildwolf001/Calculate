"""End-to-end acceptance for one-message multi-question turns.

Uses the real LLM parser/composer (requires ANTHROPIC_API_KEY). Each case asks
several causally-linked sub-questions in one message, then the script checks:

- every planned sub-question was actually handled (no silent drops);
- each query sub-question executed or was rejected with a reason;
- no INTERNAL_ERROR / SHARE_CHECK_FAILED;
- expected inherited filters are present in every query plan;
- the merged answer reports all sub-questions.

Run: python scripts/e2e_subquestions.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.controller import Controller  # noqa: E402
from app.core.event_store import EventStore  # noqa: E402
from app.domain.resolver import RuleGrounder  # noqa: E402
from app.domain.semantic.loader import load_semantic  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.llm.compose_answer import LLMComposer  # noqa: E402
from app.llm.parse_intent import LLMIntentParser  # noqa: E402

CASES = [
    {
        "id": "A",
        "name": "品类归因链",
        "question": (
            "2025年4月已送达订单的商品销售额比3月变化多少？"
            "其中数码家电贡献了多少增量？"
            "数码家电的增长是订单数变多还是客单价变高带来的？"
        ),
        "min_subs": 3,
        "expect_filter_dims": set(),
    },
    {
        "id": "B",
        "name": "时间递进链",
        "question": (
            "2025年3月到5月，商品销售额每月分别是多少？"
            "4月比3月增长了多少？"
            "5月比4月是继续增长还是回落？"
        ),
        "min_subs": 3,
        "expect_filter_dims": set(),
    },
    {
        "id": "C",
        "name": "指标驱动链",
        "question": (
            "2025年4月订单数比3月变化多少？"
            "同期客单价变化多少？"
            "销售额的变化主要是订单数还是客单价驱动的？"
        ),
        "min_subs": 3,
        "expect_filter_dims": set(),
    },
    {
        "id": "D",
        "name": "实体筛选继承",
        "question": (
            "空气炸锅2025年4月商品销售额比3月变化多少？"
            "空气炸锅4月的订单数是多少？"
            "空气炸锅4月的客单价是多少？"
        ),
        "min_subs": 3,
        "expect_filter_dims": {"product"},
    },
    {
        "id": "E",
        "name": "混合意图归因",
        "question": (
            "4月销售额增长主要来自哪个品类？"
            "这个品类增长的原因是什么？"
            "能否确定是促销带来的？"
        ),
        "min_subs": 3,
        "expect_filter_dims": set(),
    },
    {
        "id": "F",
        "name": "比率指标链",
        "question": (
            "2025年4月延迟率是多少？"
            "比3月变化多少？"
            "哪个商家延迟率最高？"
        ),
        "min_subs": 3,
        "expect_filter_dims": set(),
    },
]


async def _consume(ctrl, session_id, text):
    return [ev async for ev in ctrl.run_turn(session_id, text)]


def _plan_filters(ev) -> dict:
    plan = (ev.payload or {}).get("plan") or {}
    return plan.get("filters") or {}


def _describe(ev) -> str:
    p = ev.payload or {}
    plan = p.get("plan") or {}
    metric = plan.get("metric") or plan.get("list_dim") or "list"
    dims = plan.get("dimensions") or []
    cmp_ = plan.get("comparison")
    period = ""
    if cmp_:
        period = (f" {cmp_.get('current', {}).get('start')}~"
                  f"{cmp_.get('current', {}).get('end')}")
    t = plan.get("time")
    if t and not cmp_:
        period = f" {t.get('start')}~{t.get('end')}"
    filters = ",".join(sorted(_plan_filters(ev))) or "-"
    return (f"{metric} dims={dims or '-'}{period} filters={filters} "
            f"rows={p.get('row_count')} cols={p.get('columns')}")


def run_case(case: dict, sem, client) -> tuple[bool, list[str]]:
    store = EventStore(Path(tempfile.mkdtemp(prefix=f"e2e-{case['id']}-")))
    ctrl = Controller(store, sem, LLMIntentParser(sem, client=client),
                      RuleGrounder(sem), LLMComposer(client=client))
    events = asyncio.run(_consume(ctrl, "s1", case["question"]))
    state = store.replay("s1")
    failures: list[str] = []

    planned_ev = next((e for e in events if e.type == "subquestions.planned"), None)
    planned = ((planned_ev.payload or {}).get("items") if planned_ev else []) or []
    planned_ids = [i["id"] for i in planned]

    handled: dict[str, list[str]] = {}
    for ev in events:
        p = ev.payload or {}
        sid = p.get("subquestion_id")
        if ev.type in {"sql.executed", "subquestion.answered", "subquestion.rejected"} and sid:
            handled.setdefault(sid, []).append(ev.type)

    executed = [e for e in events if e.type == "sql.executed"]
    rejected = [e for e in events if e.type == "subquestion.rejected"]
    answers = [e for e in events if e.type == "answer.composed"]
    errors = [e for e in events if e.type == "error.raised"]
    check_issues = [
        i for e in events if e.type == "checks.completed"
        for i in (e.payload or {}).get("issues", [])
        if i.get("id") in {"SHARE_CHECK_FAILED", "AT_LIMIT"}
    ]

    print(f"\n=== {case['id']} {case['name']} ===")
    print("Q:", case["question"])
    print("planned:", len(planned), [(i["id"], i.get("intent_type")) for i in planned])
    for ev in executed:
        print("  exec", (ev.payload or {}).get("interpretation_id"), _describe(ev))
    for ev in rejected:
        print("  reject", (ev.payload or {}).get("subquestion_id"),
              (ev.payload or {}).get("reason"))
    print("status:", state.status.value)

    if len(planned) < case["min_subs"]:
        failures.append(f"只拆出 {len(planned)} 个子问题，期望至少 {case['min_subs']}")
    if not planned:
        failures.append("没有触发多子问题路径（可能退化成单计划）")
    for sid in planned_ids:
        if not handled.get(sid):
            failures.append(f"{sid} 被静默丢弃（无执行/回答/拒绝事件）")
        if handled.get(sid, []).count("sql.executed") > 1:
            failures.append(f"{sid} 被执行了多次")
    if any(e.payload.get("code") == "INTERNAL_ERROR" for e in errors):
        failures.append("出现 INTERNAL_ERROR")
    if check_issues:
        failures.append(f"校验问题: {[i['id'] for i in check_issues]}")

    # Inherited filters: every SQL-bearing plan must carry the expected dims.
    for ev in executed:
        missing = case["expect_filter_dims"] - set(_plan_filters(ev))
        if missing:
            failures.append(
                f"{(ev.payload or {}).get('interpretation_id')} 缺少继承筛选 {sorted(missing)}"
            )

    if case["min_subs"] >= 2:
        text = (answers[0].payload.get("text") if answers else "") or ""
        if f"你这次一共问了 {len(planned)} 个问题" not in text:
            failures.append("合并回答未按子问题数量生成标题")
        print("answer chars:", len(text))
        print(text[:900])
    if state.status.value != "DONE":
        failures.append(f"最终状态 {state.status.value} 不是 DONE")
    return not failures, failures


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="", help="逗号分隔的用例 id，如 A,C,F")
    ap.add_argument("--verbose", action="store_true", help="保留 INFO 日志")
    args = ap.parse_args()
    selected_ids = {s.strip().upper() for s in args.cases.split(",") if s.strip()}
    cases = [c for c in CASES if not selected_ids or c["id"] in selected_ids]

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not args.verbose:
        logging.getLogger("agent").setLevel(logging.ERROR)
    sem = load_semantic()
    client = LLMClient()
    if not client.available:
        print("LLM 不可用：请配置 ANTHROPIC_API_KEY")
        return 2
    print("model:", client.model)

    all_failures: dict[str, list[str]] = {}
    for case in cases:
        ok, failures = run_case(case, sem, client)
        if not ok:
            all_failures[case["id"]] = failures
            for f in failures:
                print("  FAIL:", f)

    print("\n" + "=" * 60)
    if all_failures:
        print("FAILED:", ", ".join(all_failures))
        return 1
    print(f"PASSED: {len(cases)} / {len(cases)} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
