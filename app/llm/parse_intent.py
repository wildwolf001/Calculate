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
from app.llm.client import LLMClient, LLMError, LLMUnavailable
from app.llm.rule_parser import RuleParser
from app.llm.schema_card import build_schema_card

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class IntentParseError(RuntimeError):
    pass


def _template(name: str) -> Template:
    return Template((PROMPT_DIR / name).read_text(encoding="utf-8"))


class LLMIntentParser:
    def __init__(self, sem: SemanticLayer, client: LLMClient | None = None,
                 fallback: RuleParser | None = None,
                 dim_values_path=DIM_VALUES_PATH):
        self.sem = sem
        self.client = client or LLMClient()
        self.fallback = fallback or RuleParser(sem, dim_values_path)
        self._system = _template("parse_intent_system.txt")
        self._user = _template("parse_intent_user.txt")
        self._schema_card = build_schema_card(sem, dim_values_path=dim_values_path)

    # ---------- validation ----------
    ALLOWED_SLOT_KEYS = {
        "metric", "dimensions", "time", "filters", "having", "compare",
        "comparison", "order_by", "limit", "nested", "intent", "unresolved", "notes",
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

        metric = raw_slots.get("metric")
        if metric is not None:
            if metric not in self.sem.metrics:
                by_name = [m.id for m in self.sem.metrics.values() if m.name == metric]
                if by_name:
                    metric = by_name[0]
                else:
                    raise IntentParseError(f"未知指标: {metric}")

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

        return {
            "metric": metric,
            "dimensions": dims,
            "time": time,
            "filters": filters,
            "having": raw_slots.get("having") or [],
            "compare": raw_slots.get("compare") or None,
            "comparison": comparison,
            "order_by": raw_slots.get("order_by") or [],
            "limit": int(raw_slots.get("limit", 100)),
            "nested": raw_slots.get("nested") or None,
            "intent": raw_slots.get("intent", "query"),
            "unresolved": raw_slots.get("unresolved") or [],
            "notes": [],
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
                    entry["intent_type"] = "explain"
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
            "query", "analysis", "explain", "unsupported", "smalltalk", "lookup",
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
    def parse(self, text: str, context: dict) -> ParseResult:
        if self.client.available:
            base_user = self._user.substitute(
                prev_slots=json.dumps(context.get("slots") or {}, ensure_ascii=False),
                recent_messages=json.dumps(context.get("recent_messages") or [], ensure_ascii=False),
                conversation_context=json.dumps(
                    context.get("conversation_context") or {},
                    ensure_ascii=False,
                    default=str,
                ),
                question=text,
            )
            system = self._system.substitute(schema_card=self._schema_card)
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
                    data = self.client.complete_json(system, user, max_tokens=3600,
                                                     stage="parse_intent")
                    result = self._validate(data)
                    result = self._postprocess_lookup(result, text)
                    result.notes = list(dict.fromkeys(result.notes + ["由模型解析意图"]))
                    return result
                except (LLMError, LLMUnavailable, IntentParseError, ValueError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log_event(logger, "parse_intent.invalid", attempt=attempt + 1,
                              error=last_error)
            fallback = self.fallback.parse(text, context)
            fallback.notes = list(dict.fromkeys(
                fallback.notes + [
                    f"模型解析失败，已使用规则解析：{last_error}",
                ]
            ))
            return fallback
        result = self.fallback.parse(text, context)
        result.notes = list(dict.fromkeys(result.notes + ["离线规则解析"]))
        return result
