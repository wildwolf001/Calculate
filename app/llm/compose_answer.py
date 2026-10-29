"""Answer composition.

`TemplateComposer` is the deterministic fallback. `LLMComposer` adds natural
language but is gated by a number whitelist: if the model emits a number that
is not derivable from the result/checks, the template answer is used instead.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from app.core.logging import log_event, logger


def fmt_number(value, unit: str | None = None) -> str:
    if value is None:
        return "—"
    if unit == "%":
        return f"{float(value) * 100:.2f}%"
    if isinstance(value, float):
        if unit in {"元", "分"}:
            return f"{value:,.2f}"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,}" if isinstance(value, int) else str(value)


def _time_label(ctx: dict) -> str:
    t = ctx.get("time")
    if not t:
        return ""
    return f"{t['start']} 至 {t['end']}（不含右端）"


def _checks_block(checks: list[dict]) -> str:
    seen = set()
    lines = []
    for c in checks:
        key = (c.get("title"), c.get("detail"))
        if key in seen:
            continue
        seen.add(key)
        detail = c.get("detail") or ""
        lines.append(f"- {c.get('title', '')}{('：' + detail) if detail else ''}")
    return "\n".join(lines)


class TemplateComposer:
    def compose(self, ctx: dict) -> str:
        if ctx.get("kind") == "subquestions":
            return self._compose_subquestions(ctx)
        interps = ctx.get("interpretations") or []
        if len(interps) > 1:
            return self._compose_multi(ctx, interps)
        return self._compose_single(ctx)

    def _compose_subquestions(self, ctx: dict) -> str:
        items = ctx.get("subquestions") or []
        parts = [
            f"你这次一共问了 {ctx.get('question_count', len(items))} 个问题，"
            "结果如下："
        ]
        for item in items:
            question = item.get("question") or f"问题 {item.get('index', '')}"
            if item.get("context"):
                body = self._compose_single(item["context"])
            else:
                body = item.get("text") or "这个问题暂时无法回答。"
            parts.append(f"**{item.get('index', '')}. {question}**\n\n{body}")
        return "\n\n".join(parts)

    def _compose_multi(self, ctx: dict, interps: list[dict]) -> str:
        parts = ["你这句话有多种口径，结果都列在下面："]
        ordered = sorted(interps, key=lambda i: (not i.get("preferred"), -(i.get("confidence") or 0)))
        for idx, interp in enumerate(ordered, 1):
            label = interp.get("label") or interp.get("id") or f"口径{idx}"
            body = self._compose_single(self._as_single(interp))
            parts.append(f"**{idx}. {label}**\n{body}")
        if ctx.get("ambiguity_note"):
            parts.append(f"说明：{ctx['ambiguity_note']}")
        return "\n\n".join(parts)

    @staticmethod
    def _as_single(interp: dict) -> dict:
        single = dict(interp)
        comparison = interp.get("comparison")
        meta = interp.get("meta") or {}
        single["compare"] = bool(comparison or meta.get("current_range"))
        single["comparison"] = comparison
        if not single.get("time") and comparison:
            single["time"] = comparison.get("current")
        single["facts"] = interp.get("facts") or {}
        single["checks"] = interp.get("checks") or []
        return single

    def _compose_single(self, ctx: dict) -> str:
        metric_name = ctx.get("metric_name") or ctx.get("metric") or "结果"
        unit = ctx.get("metric_unit")
        mtype = ctx.get("metric_type", "aggregate")
        dims = ctx.get("dimension_labels") or []
        rows = ctx.get("rows") or []
        cols = ctx.get("columns") or []
        checks = ctx.get("checks") or []
        facts = ctx.get("facts") or {}
        time_label = _time_label(ctx)

        parts: list[str] = []
        subject = ctx.get("filters_label") or ""
        scope = "，".join(x for x in [time_label, subject] if x)
        scope = f"{scope}的" if scope else ""

        if not rows:
            parts.append(f"{scope}{metric_name}没有数据，无法得出结论。")
            diagnosis = facts.get("empty_diagnosis") or {}
            if diagnosis.get("message"):
                parts.append(diagnosis["message"])
        elif ctx.get("compare"):
            current_idx = cols.index("current") if "current" in cols else 0
            base_idx = cols.index("baseline") if "baseline" in cols else 1
            delta_idx = cols.index("delta") if "delta" in cols else None
            growth_idx = cols.index("growth_rate") if "growth_rate" in cols else 3
            comparison = ctx.get("comparison") or {}
            label = comparison.get("label")
            period_txt = f"{label}，" if label else ""
            if dims:
                ordered = sorted(
                    rows,
                    key=lambda row: (
                        row[delta_idx] is None if delta_idx is not None else True,
                        -(float(row[delta_idx])
                          if delta_idx is not None and row[delta_idx] is not None
                          else 0.0),
                    ),
                )
                display = []
                for i, row in enumerate(ordered[:5], 1):
                    cur = fmt_number(row[current_idx], unit)
                    base = fmt_number(row[base_idx], unit)
                    delta = (
                        f"，变化 {fmt_number(row[delta_idx], unit)}"
                        if delta_idx is not None else ""
                    )
                    growth = row[growth_idx]
                    growth_txt = (
                        f"，增长 {float(growth) * 100:.2f}%"
                        if growth is not None and float(growth) >= 0
                        else f"，下降 {abs(float(growth)) * 100:.2f}%"
                        if growth is not None
                        else "，增长率无法计算"
                    )
                    name = " / ".join(str(value) for value in row[:len(dims)])
                    display.append(
                        f"{i}. {name}：本期 {cur}，基期 {base}{delta}{growth_txt}"
                    )
                more = (
                    f"（共 {ctx.get('row_count')} 组，展示前 {len(display)} 组）"
                    if len(rows) > len(display) else ""
                )
                parts.append(
                    f"{period_txt}{scope}{metric_name}按"
                    f"{'、'.join(dims)}{more}如下：\n" + "\n".join(display)
                )
            else:
                first = rows[0]
                cur = fmt_number(first[current_idx], unit)
                base = fmt_number(first[base_idx], unit)
                delta = (
                    f"，变化 {fmt_number(first[delta_idx], unit)}"
                    if delta_idx is not None else ""
                )
                growth = first[growth_idx]
                if growth is None:
                    parts.append(
                        f"{period_txt}{scope}{metric_name}本期为 {cur}，"
                        f"基期为 {base}{delta}；基期为空或 0，无法计算增长率。"
                    )
                else:
                    parts.append(
                        f"{period_txt}{scope}{metric_name}本期为 {cur}，"
                        f"基期为 {base}{delta}，"
                        f"{'增长' if float(growth) >= 0 else '下降'} "
                        f"{abs(float(growth)) * 100:.2f}%。"
                    )
        elif not dims:
            first = rows[0]
            value_idx = cols.index("value") if "value" in cols else len(first) - 1
            value = first[value_idx]
            extra = ""
            if mtype == "ratio" and "numerator" in cols and "denominator" in cols:
                num = fmt_number(first[cols.index("numerator")], None)
                den = fmt_number(first[cols.index("denominator")], None)
                extra = f"（{num}/{den}）"
            parts.append(f"{scope}{metric_name}为 {fmt_number(value, unit)}{extra}。")
        elif ctx.get("per_group_top") and len(dims) >= 2:
            group_label = dims[0]
            item_index = len(dims) - 1
            value_idx = cols.index("value") if "value" in cols else None
            display = []
            for i, row in enumerate(ctx["per_group_top"], 1):
                item = row[item_index] if len(row) > item_index else ""
                val = (
                    fmt_number(row[value_idx], unit)
                    if value_idx is not None else ""
                )
                display.append(f"{i}. {row[0]}：{item}（{val}）")
            more = ""
            if ctx.get("rows_truncated"):
                more = f"（结果共 {ctx.get('rows_total')} 行）"
            parts.append(
                f"{scope}{metric_name}按{group_label}统计，"
                f"每个{group_label}的第一名如下{more}：\n"
                + "\n".join(display)
            )
        else:
            display = []
            for i, row in enumerate(rows[:5], 1):
                name = row[0]
                value = row[cols.index("value")] if "value" in cols else row[-1]
                share_txt = ""
                if "share" in cols:
                    share_value = row[cols.index("share")]
                    if share_value is not None:
                        share_txt = f"（占比 {float(share_value) * 100:.2f}%）"
                display.append(
                    f"{i}. {name}：{fmt_number(value, unit)}{share_txt}"
                )
            more = f"（共 {ctx.get('row_count')} 组，展示前 {min(5, len(rows))} 组）"
            parts.append(
                f"{scope}{metric_name}按{ '、'.join(dims) }如下{more}：\n"
                + "\n".join(display)
            )

        if facts.get("share_of_total") is not None and dims:
            parts.append(f"各分组合计占总体 {facts['share_of_total'] * 100:.2f}%。")

        members = facts.get("member_values") or []
        if members and not dims and not ctx.get("compare"):
            label = facts.get("member_dimension_name") or "成员"
            shown = "、".join(str(value) for value in members[:20])
            suffix = ""
            if facts.get("member_values_truncated") or len(members) > 20:
                suffix = "，其余成员请在完整结果表中查看"
            parts.append(f"具体{label}包括：{shown}{suffix}。")

        top = facts.get("top_delta_member")
        if top and dims:
            growth = top.get("growth_rate")
            growth_txt = (
                f"，增长率 {float(growth) * 100:.2f}%"
                if growth is not None else ""
            )
            parts.append(
                f"增量贡献最大的是 **{top.get('value')}**，"
                f"变化 {fmt_number(top.get('delta'), unit)}{growth_txt}。"
            )
            total_delta = facts.get("comparison_delta_total")
            share = facts.get("top_contribution_share")
            if total_delta is not None:
                summary = (
                    f"所选分组净变化合计为 {fmt_number(total_delta, unit)}"
                )
                if share is not None:
                    summary += (
                        f"；最大贡献项占净变化的 {float(share) * 100:.2f}%。"
                    )
                    if abs(float(share)) > 1:
                        summary += "该比例超过 100%，表示其他分组存在负增长。"
                else:
                    summary += "。"
                parts.append(summary)
        negative = facts.get("largest_negative_contributor")
        if negative and dims:
            parts.append(
                f"最大拖累项为 **{negative.get('value')}**，"
                f"变化 {fmt_number(negative.get('delta'), unit)}。"
            )

        block = _checks_block(checks)
        if block:
            parts.append("口径与数据说明：\n" + block)

        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM composer with number whitelist
# ---------------------------------------------------------------------------
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _collect_numbers(value, out: list[float]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
    elif isinstance(value, str):
        for token in re.findall(r"-?\d+(?:\.\d+)?", value):
            try:
                out.append(float(token))
            except ValueError:
                pass
    elif isinstance(value, dict):
        for v in value.values():
            _collect_numbers(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect_numbers(v, out)


def allowed_numbers(ctx: dict) -> set[str]:
    raw: list[float] = []
    _collect_numbers(ctx.get("rows"), raw)
    _collect_numbers(ctx.get("interpretations"), raw)
    _collect_numbers(ctx.get("sections"), raw)
    _collect_numbers(ctx.get("checks"), raw)
    _collect_numbers(ctx.get("facts"), raw)
    _collect_numbers(ctx.get("row_count"), raw)
    _collect_numbers(ctx.get("time"), raw)
    _collect_numbers(ctx.get("notes"), raw)
    _collect_numbers(ctx.get("subquestions"), raw)
    # 100 is a conventional percentage threshold (for example, contribution
    # above 100% when other groups decline), not a new business fact.
    allowed: set[str] = {"100"}
    for v in raw:
        variants = {v, abs(v), v * 100, v / 100, v / 10000, v / 1e8, round(v, 2)}
        for x in variants:
            allowed.add(f"{x:.2f}".rstrip("0").rstrip("."))
            allowed.add(f"{int(x)}" if float(x).is_integer() else f"{x:g}")
    return allowed


def numbers_are_grounded(text: str, ctx: dict) -> bool:
    allowed = allowed_numbers(ctx)
    allowed_vals = {float(a) for a in allowed if re.fullmatch(r"-?\d+(?:\.\d+)?", a)}
    for match in re.finditer(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text):
        token = match.group(0).replace(",", "")
        try:
            value = float(token)
        except ValueError:
            continue
        if abs(value) <= 30 and float(value).is_integer():
            continue  # ranking ordinals
        if token in allowed:
            continue
        if any(abs(value - a) <= max(0.01, abs(a) * 0.002) for a in allowed_vals):
            continue
        return False
    return True


class LLMComposer:
    def __init__(self, client=None, fallback: TemplateComposer | None = None):
        from app.llm.client import LLMClient
        self.client = client or LLMClient()
        self.fallback = fallback or TemplateComposer()
        self._system = Template((PROMPT_DIR / "compose_answer_system.txt").read_text(encoding="utf-8"))
        self._user = Template((PROMPT_DIR / "compose_answer_user.txt").read_text(encoding="utf-8"))
        self._multi_system = Template(
            (PROMPT_DIR / "compose_subquestions_system.txt")
            .read_text(encoding="utf-8")
        )
        self._multi_user = Template(
            (PROMPT_DIR / "compose_subquestions_user.txt")
            .read_text(encoding="utf-8")
        )

    def compose(self, ctx: dict) -> str:
        if not self.client.available:
            return self.fallback.compose(ctx)
        multi = ctx.get("kind") == "subquestions"
        try:
            if multi:
                system = self._multi_system.substitute()
                user = self._multi_user.substitute(
                    context=json.dumps(ctx, ensure_ascii=False, default=str)
                )
                budget = 4000
            else:
                system = self._system.substitute()
                user = self._user.substitute(context=json.dumps(ctx, ensure_ascii=False, default=str))
                budget = 3000 if ctx.get("interpretations") else 2000
            text = self.client.complete_text(system, user, max_tokens=budget,
                                             stage="compose_answer")
            if not text.strip():
                raise ValueError("empty answer")
            if not numbers_are_grounded(text, ctx):
                raise ValueError("answer contains ungrounded numbers")
            return text.strip()
        except Exception as exc:
            log_event(
                logger,
                "composer.llm_failed",
                stage="compose_subquestions" if multi else "compose_answer",
                error=f"{type(exc).__name__}: {exc}",
            )
            return self.fallback.compose(ctx)
