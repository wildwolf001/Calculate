"""LLM-1: natural language -> slots. Structured output is validated; any
failure falls back to the deterministic rule parser (and is recorded)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from app.config.settings import DIM_VALUES_PATH, MAX_SUBQUESTIONS
from app.core.interfaces import ParseResult
from app.core.logging import log_event, logger
from app.domain.plan import ComparisonSpec
from app.domain.semantic.loader import SemanticLayer
from app.agent.contracts import ToolContext
from app.chat.agent.tools import DescribeMetricTool, SearchSemanticsTool
from app.llm.client import LLMClient, LLMError, LLMUnavailable
from app.llm.rule_parser import RuleParser
from app.llm.schema_card import build_schema_card
from app.llm.semantic_brief import build_brief

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class IntentParseError(RuntimeError):
    pass


def _template(name: str) -> Template:
    return Template((PROMPT_DIR / name).read_text(encoding="utf-8"))


class LLMIntentParser:
    def __init__(self, sem: SemanticLayer, client: LLMClient | None = None,
                 fallback: RuleParser | None = None,
                 dim_values_path=DIM_VALUES_PATH,
                 index: dict | None = None,
                 use_brief: bool = True):
        self.sem = sem
        self.client = client or LLMClient()
        self.fallback = fallback or RuleParser(sem, dim_values_path)
        self._system = _template("parse_intent_system.txt")
        self._user = _template("parse_intent_user.txt")
        self._schema_card = build_schema_card(sem, dim_values_path=dim_values_path)
        self._index = index or {}
        self._use_brief = bool(use_brief and self._index)

    def _tool_context(self, text: str) -> ToolContext:
        return ToolContext(data={"index": self._index, "question": text})

    def _apply_semantic_tools(self, text: str, result: ParseResult) -> ParseResult:
        """Use chat-side tools to enrich or rescue a parsed query.

        This is deliberately conservative: it only adds a metric when exactly
        one candidate name occurs in the question, and never invents one.
        """
        if not self._index or result.intent_type != "query":
            return result
        slots = dict(result.slots or {})
        metric_id = slots.get("metric")
        metrics = self._index.get("metrics") or {}
        if metric_id and metric_id in metrics:
            described = DescribeMetricTool().run(
                {"metric_id": metric_id}, self._tool_context(text),
            )
            if described.ok:
                expr = (described.facts.get("metric") or {}).get("expr")
                if expr:
                    result.notes = list(dict.fromkeys(
                        result.notes + [f"工具口径：{expr}"]
                    ))
            return result
        if slots.get("metric_expr"):
            return result
        searched = SearchSemanticsTool().run(
            {"query": text, "limit": 5}, self._tool_context(text),
        )
        if not searched.ok:
            return result
        candidates = searched.facts.get("metrics") or []
        exact = [
            item for item in candidates
            if item.get("name") and str(item["name"]) in text
        ]
        if len(exact) == 1:
            chosen = exact[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            return result
        slots["metric"] = chosen["id"]
        slots["metric_source"] = "documented"
        slots["unresolved"] = [
            term for term in (slots.get("unresolved") or [])
            if str(chosen.get("name")) not in str(term)
        ]
        result.slots = slots
        result.unresolved = list(slots["unresolved"])
        result.notes = list(dict.fromkeys(
            result.notes + [f"工具检索指标：{chosen.get('name')}"]
        ))
        return result

    def _system_for(self, text: str):
        card = self._schema_card
        if self._use_brief:
            brief = build_brief(self._index, text)
            if brief:
                card = brief
        return self._system.substitute(schema_card=card)

    # ---------- validation ----------
    ALLOWED_SLOT_KEYS = {
        "metric", "dimensions", "time", "filters", "having", "compare",
        "comparison", "order_by", "limit", "nested", "intent", "unresolved", "notes",
        "coverage_period", "ratio_numerator", "ratio_denominator", "time_axis",
        "share", "limit_per_group", "metric_expr", "metric_source",
    }

    def _range(self, value: dict, name: str) -> dict:
        if not (isinstance(value, dict) and value.get("start") and value.get("end")):
            raise IntentParseError(f"{name} 必须是 {{start,end}}")
        lo, hi = self.sem.time_range
        start, end = str(value["start"]), str(value["end"])
        if start < lo:
            start = lo
        if end > hi:
            end = hi
        if start >= end:
            raise IntentParseError(f"{name} 区间为空")
        return {"start": start, "end": end}

    def _map_dimension(self, value: str) -> str:
        if value in self.sem.dimensions:
            return value
        target = str(value or "").lower()
        for dim in self.sem.dimensions.values():
            names = [dim.id, dim.raw.get("name_zh", ""), *dim.raw.get("synonyms", [])]
            if target and target in {str(n).lower() for n in names if n}:
                return dim.id
        raise IntentParseError(f"未知维度: {value}")

    def _validate_slots(self, raw_slots: dict) -> dict:
        if not isinstance(raw_slots, dict):
            raise IntentParseError("slots 不是对象")
        unknown = set(raw_slots) - self.ALLOWED_SLOT_KEYS
        if unknown:
            raise IntentParseError(f"未知槽位字段: {sorted(unknown)}")

        metric_expr = raw_slots.get("metric_expr") or None
        metric = raw_slots.get("metric")
        if metric_expr is not None:
            if not isinstance(metric_expr, dict):
                raise IntentParseError("metric_expr 必须是对象")
            from app.domain.query.metric import build_metric_spec

            try:
                metric_expr = build_metric_spec(
                    metric_expr, self.sem, label=metric_expr.get("label")
                )
            except Exception as exc:
                raise IntentParseError(f"metric_expr 非法: {exc}") from exc
            metric = metric_expr["id"]
        elif metric is not None:
            if metric not in self.sem.metrics:
                by_name = [m.id for m in self.sem.metrics.values() if m.name == metric]
                if by_name:
                    metric = by_name[0]
                else:
                    raise IntentParseError(f"未知指标: {metric}")

        ratio_numerator = raw_slots.get("ratio_numerator") or None
        ratio_denominator = raw_slots.get("ratio_denominator") or None
        if bool(ratio_numerator) != bool(ratio_denominator):
            raise IntentParseError("ratio_numerator 和 ratio_denominator 必须同时提供")
        if ratio_numerator is not None:
            ratio_numerator = str(ratio_numerator)
            ratio_denominator = str(ratio_denominator)
            for rid in (ratio_numerator, ratio_denominator):
                if rid not in self.sem.metrics:
                    raise IntentParseError(f"未知指标: {rid}")
            metric = ratio_numerator

        dims = raw_slots.get("dimensions") or []
        if not isinstance(dims, list):
            raise IntentParseError("dimensions 必须是数组")
        dims = [self._map_dimension(d) for d in dims]

        filters = {}
        for dim, spec in (raw_slots.get("filters") or {}).items():
            dim = self._map_dimension(dim)
            if isinstance(spec, dict):
                filters[dim] = {"op": spec.get("op", "eq"), "value": spec.get("value")}
            else:
                filters[dim] = {"op": "eq", "value": spec}

        time_axis = raw_slots.get("time_axis") or None
        if time_axis is not None and not isinstance(time_axis, str):
            raise IntentParseError("time_axis 必须是字符串或 null")

        time = None
        if raw_slots.get("time"):
            time = self._range(raw_slots["time"], "time")

        comparison = None
        raw_comp = raw_slots.get("comparison")
        if raw_comp:
            if not isinstance(raw_comp, dict):
                raise IntentParseError("comparison 必须是对象")
            comparison = {
                "label": raw_comp.get("label"),
                "mode": raw_comp.get("mode", "period_vs_period"),
                "current": self._range(raw_comp.get("current") or {}, "comparison.current"),
                "baseline": self._range(raw_comp.get("baseline") or {}, "comparison.baseline"),
            }
            try:
                ComparisonSpec.model_validate(comparison)
            except Exception as exc:
                raise IntentParseError(f"comparison 非法: {exc}") from exc

        compare = raw_slots.get("compare") or None
        if isinstance(compare, str):
            if compare not in {"prev_period", "prev_year"}:
                raise IntentParseError(f"未知比较类型: {compare}")
            compare = {"type": compare}
        elif compare is not None and not isinstance(compare, dict):
            raise IntentParseError("compare 必须是对象、prev_period、prev_year 或 null")

        share = bool(raw_slots.get("share", False))

        raw_limit_per_group = raw_slots.get("limit_per_group")
        limit_per_group = None
        if raw_limit_per_group is not None:
            try:
                limit_per_group = int(raw_limit_per_group)
            except (TypeError, ValueError) as exc:
                raise IntentParseError("limit_per_group 必须是整数") from exc
            if not 1 <= limit_per_group <= 50:
                raise IntentParseError("limit_per_group 必须在 1..50")

        return {
            "metric": metric,
            "metric_expr": metric_expr,
            "metric_source": raw_slots.get("metric_source") or (
                "inferred" if metric_expr else None
            ),
            "ratio_numerator": ratio_numerator,
            "ratio_denominator": ratio_denominator,
            "dimensions": dims,
            "time": time,
            "time_axis": time_axis,
            "filters": filters,
            "having": raw_slots.get("having") or [],
            "compare": compare,
            "comparison": comparison,
            "order_by": raw_slots.get("order_by") or [],
            "limit": int(raw_slots.get("limit", 100)),
            "limit_per_group": limit_per_group,
            "share": share,
            "nested": raw_slots.get("nested") or None,
            "intent": raw_slots.get("intent", "query"),
            "unresolved": raw_slots.get("unresolved") or [],
            "notes": [],
            "coverage_period": raw_slots.get("coverage_period", "month"),
        }

    def _validate_subquestions(self, raw: list) -> list[dict]:
        if not isinstance(raw, list):
            raise IntentParseError("subquestions 必须是数组")
        out: list[dict] = []
        for item in raw[:MAX_SUBQUESTIONS]:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            has_slots = bool(item.get("slots"))
            intent = item.get("intent_type") or (
                "query" if has_slots
                else ("unsupported" if item.get("explanation") else "query")
            )
            if intent not in {"query", "lookup", "explain", "unsupported", "smalltalk"}:
                intent = "query" if has_slots else "unsupported"
            entry = {
                "question": question[:300],
                "intent_type": intent,
                "slots": {},
                "explanation": item.get("explanation"),
            }
            raw_context = item.get("context") if isinstance(item.get("context"), dict) else {}
            action = str(raw_context.get("action") or "new").lower()
            action = {
                "new_query": "new",
                "inherit_scope": "inherit",
                "refine_query": "refine",
                "result_reference": "reference_result",
            }.get(action, action)
            entry["context_action"] = (
                action if action in {"new", "inherit", "refine", "reference_result"}
                else "new"
            )
            if intent in {"query", "lookup"}:
                try:
                    entry["slots"] = self._validate_slots(item.get("slots") or {})
                except IntentParseError as exc:
                    # Do not silently turn a malformed query into "explain":
                    # that hides the failure and produces a definition essay.
                    # Keep it a query and surface the error to the controller.
                    entry["intent_type"] = "query"
                    entry["slots"] = {}
                    entry["error"] = str(exc)
            if intent == "lookup" and entry["intent_type"] == "lookup":
                try:
                    entry["lookup_dimension"] = self._map_dimension(
                        str(item.get("lookup_dimension") or "")
                    )
                except IntentParseError as exc:
                    entry["intent_type"] = "explain"
                    entry["error"] = str(exc)
            out.append(entry)
        return out

    def _validate(self, data: dict) -> ParseResult:
        if not isinstance(data, dict):
            raise IntentParseError("输出不是 JSON 对象")
        subquestions = self._validate_subquestions(data.get("subquestions") or [])
        intent_type = data.get("intent_type", "query")
        if intent_type not in {
            "query", "analysis", "coverage", "critique_response",
            "explain", "unsupported", "smalltalk", "lookup",
        }:
            if len(subquestions) < 2:
                raise IntentParseError(f"未知 intent_type: {intent_type}")
            intent_type = "query"
        tier = data.get("tier", "A")
        if tier not in {"A", "B", "C"}:
            tier = "A"

        viz = data.get("visualization") if isinstance(data.get("visualization"), dict) else None
        try:
            slots = self._validate_slots(data.get("slots") or {})
        except IntentParseError:
            if len(subquestions) < 2:
                raise
            slots = self._validate_slots({})
        slots["notes"] = list(data.get("notes") or [])
        slots["visualization"] = viz
        if len(subquestions) >= 2:
            intent_type = "query"
        raw_context = data.get("context") if isinstance(data.get("context"), dict) else {}
        context_action = str(
            raw_context.get("action")
            or data.get("context_action")
            or "new"
        ).strip().lower()
        aliases = {
            "new_query": "new",
            "inherit_scope": "inherit",
            "refine_query": "refine",
            "result_reference": "reference_result",
        }
        context_action = aliases.get(context_action, context_action)
        if context_action not in {"new", "inherit", "refine", "reference_result"}:
            context_action = "new"
        context_source = raw_context.get("source") or data.get("context_source")
        try:
            context_confidence = float(
                raw_context.get("confidence")
                or data.get("context_confidence")
                or 0.0
            )
        except (TypeError, ValueError):
            context_confidence = 0.0
        context_confidence = max(0.0, min(1.0, context_confidence))
        if data.get("assumptions"):
            slots["notes"] += [f"模型假设：{a}" for a in data["assumptions"]]

        interpretations = []
        if len(subquestions) < 2:
            for idx, item in enumerate(data.get("interpretations") or []):
                if not isinstance(item, dict):
                    raise IntentParseError("interpretations 元素必须是对象")
                interp_slots = self._validate_slots(item.get("slots") or {})
                interp_slots["notes"] = item.get("notes") or slots["notes"]
                interp_slots["visualization"] = item.get("visualization") or viz
                interpretations.append({
                    "id": str(item.get("id") or f"i{idx}"),
                    "label": item.get("label") or item.get("name"),
                    "confidence": float(item.get("confidence", 0.5)),
                    "slots": interp_slots,
                })
        preferred = data.get("preferred") or data.get("preferred_interpretation")
        if interpretations:
            ids = [i["id"] for i in interpretations]
            if preferred not in ids:
                preferred = max(interpretations, key=lambda i: i["confidence"])["id"]
            if not slots.get("metric"):
                chosen = next(i for i in interpretations if i["id"] == preferred)
                slots = chosen["slots"]

        lookup_dim = data.get("lookup_dimension")
        if intent_type == "lookup":
            if lookup_dim not in self.sem.dimensions:
                mapped = None
                target = str(lookup_dim or "").lower()
                for dim in self.sem.dimensions.values():
                    names = [dim.id, dim.raw.get("name_zh", ""), *dim.raw.get("synonyms", [])]
                    if target and target in {str(n).lower() for n in names if n}:
                        mapped = dim.id
                        break
                if mapped is None:
                    raise IntentParseError(f"未知 lookup_dimension: {lookup_dim}")
                lookup_dim = mapped

        return ParseResult(
            intent_type=intent_type,
            tier=tier,
            slots=slots,
            unresolved=slots["unresolved"],
            subquestions=subquestions,
            explanation=data.get("explanation"),
            notes=slots["notes"],
            confidence=float(data.get("confidence", 0.7)),
            lookup_dim=lookup_dim,
            rewritten_question=data.get("rewritten_question"),
            interpretations=interpretations,
            preferred_interpretation=preferred,
            ambiguity_note=data.get("ambiguity_note"),
            visualization=viz,
            context_action=context_action,
            context_source=str(context_source) if context_source else None,
            context_confidence=context_confidence,
        )

    def _postprocess_lookup(self, result: ParseResult, text: str) -> ParseResult:
        """“多少种/几个”通常是计数，不是成员清单。"""
        if result.intent_type != "lookup":
            return result
        match = re.search(
            r"(?:多少|几)\s*(?:种|个|款|类|位|名|家)?\s*([\u4e00-\u9fffA-Za-z_]{2,8})",
            text,
        )
        if not match:
            return result
        noun = match.group(1)
        candidates = []
        for metric in self.sem.metrics.values():
            expr = str(metric.raw.get("expr") or "").upper()
            if noun in metric.name and ("数" in metric.name or "COUNT(DISTINCT" in expr):
                candidates.append(metric)
        if not candidates:
            return result
        selected = [m for m in candidates if m.raw.get("selected", True)]
        metric = (selected or candidates)[0]
        slots = dict(result.slots or {})
        slots.update({
            "metric": metric.id,
            "dimensions": [],
            "intent": "query",
        })
        result.intent_type = "query"
        result.lookup_dim = None
        result.slots = slots
        result.unresolved = []
        result.notes = list(dict.fromkeys(
            result.notes + [f"“多少种”按计数指标「{metric.name}」理解"]
        ))
        return result

    # ---------- main ----------
    def _render_user(self, text: str, context: dict) -> str:
        return self._user.substitute(
            prev_slots=json.dumps(
                context.get("slots") or {}, ensure_ascii=False
            ),
            recent_messages=json.dumps(
                context.get("recent_messages") or [], ensure_ascii=False
            ),
            conversation_context=json.dumps(
                context.get("conversation_context") or {},
                ensure_ascii=False,
                default=str,
            ),
            question=text,
        )

    def repair(self, text: str, context: dict, *, plan: dict,
               issues: list[dict], tool_results: list[dict]) -> ParseResult | None:
        """Re-plan after deterministic validation rejects the first plan."""
        if not self.client.available:
            return None
        system = self._system_for(text)
        user = self._render_user(text, context) + (
            "\n\n<failed_plan>\n"
            + json.dumps(plan, ensure_ascii=False, default=str)
            + "\n</failed_plan>\n<validation_issues>\n"
            + json.dumps(issues, ensure_ascii=False, default=str)
            + "\n</validation_issues>\n<tool_results>\n"
            + json.dumps(tool_results, ensure_ascii=False, default=str)
            + "\n</tool_results>\n"
            "请根据上述确定性工具结果修正计划。只能使用 schema 中存在的指标、"
            "维度和数据库成员值；不要重复原错误，只输出一个完整 JSON。"
        )
        data = self.client.complete_json(
            system, user, max_tokens=4000, stage="repair_plan"
        )
        result = self._validate(data)
        result = self._postprocess_lookup(result, text)
        result = self._apply_semantic_tools(text, result)
        result.notes = list(dict.fromkeys(
            result.notes + ["由工具辅助重新规划"]
        ))
        return result

    def parse(self, text: str, context: dict) -> ParseResult:
        if self.client.available:
            base_user = self._render_user(text, context)
            system = self._system_for(text)
            last_error = ""
            # One self-correction attempt: invalid JSON / invalid slots are the
            # usual transient failures, and falling straight to the rule parser
            # silently loses lookup-shaped questions.
            for attempt in range(2):
                user = base_user
                if attempt:
                    user += (
                        "\n\n上一次输出未通过校验："
                        f"{last_error}\n请严格按 <output_schema> 重新输出一个合法 JSON。"
                    )
                try:
                    data = self.client.complete_json(system, user, max_tokens=6000,
                                                     stage="parse_intent")
                    result = self._validate(data)
                    result = self._postprocess_lookup(result, text)
                    result = self._apply_semantic_tools(text, result)
                    result.notes = list(dict.fromkeys(result.notes + ["由模型解析意图"]))
                    return result
                except (LLMError, LLMUnavailable, IntentParseError, ValueError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log_event(logger, "parse_intent.invalid", attempt=attempt + 1,
                              error=last_error)
            fallback = self.fallback.parse(text, context)
            fallback = self._apply_semantic_tools(text, fallback)
            fallback.notes = list(dict.fromkeys(
                fallback.notes + [
                    f"模型解析失败，已使用规则解析：{last_error}",
                ]
            ))
            return fallback
        result = self.fallback.parse(text, context)
        result = self._apply_semantic_tools(text, result)
        result.notes = list(dict.fromkeys(result.notes + ["离线规则解析"]))
        return result
