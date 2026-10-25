"""LLM-1: natural language -> slots. Structured output is validated; any
failure falls back to the deterministic rule parser (and is recorded)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

from app.config.settings import MAX_SUBQUESTIONS
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
                 fallback: RuleParser | None = None):
        self.sem = sem
        self.client = client or LLMClient()
        self.fallback = fallback or RuleParser(sem)
        self._system = _template("parse_intent_system.txt")
        self._user = _template("parse_intent_user.txt")
        self._schema_card = build_schema_card(sem)

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
        for d in dims:
            if d not in self.sem.dimensions:
                raise IntentParseError(f"未知维度: {d}")

        filters = {}
        for dim, spec in (raw_slots.get("filters") or {}).items():
            if dim not in self.sem.dimensions:
                raise IntentParseError(f"未知过滤维度: {dim}")
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
        """Validate each sub-question independently.

        A malformed sub-question is downgraded to an explanation instead of
        failing the whole turn: partial answers are better than none.
        """
        if not isinstance(raw, list):
            raise IntentParseError("subquestions 必须是数组")
        out: list[dict] = []
        for item in raw[:MAX_SUBQUESTIONS]:
            # One malformed item must not invalidate the whole compound turn:
            # drop it and let the remaining sub-questions run.
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
            if intent in {"query", "lookup"}:
                try:
                    entry["slots"] = self._validate_slots(item.get("slots") or {})
                except IntentParseError as exc:
                    entry["intent_type"] = "explain"
                    entry["error"] = str(exc)
            if intent == "lookup":
                entry["lookup_dimension"] = item.get("lookup_dimension")
            out.append(entry)
        return out

    def _validate(self, data: dict) -> ParseResult:
        if not isinstance(data, dict):
            raise IntentParseError("输出不是 JSON 对象")
        subquestions = self._validate_subquestions(data.get("subquestions") or [])
        intent_type = data.get("intent_type", "query")
        if intent_type not in {"query", "analysis", "explain", "unsupported", "smalltalk", "lookup"}:
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
            # A compound turn does not need a coherent top-level plan.
            slots = self._validate_slots({})
        slots["notes"] = list(data.get("notes") or [])
        slots["visualization"] = viz

        if len(subquestions) >= 2:
            # A compound message is one turn that carries several questions;
            # the top-level intent describes the container, not any one part.
            intent_type = "query"
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
            if lookup_dim not in {"category", "seller", "product", "state", "city", "payment_type"}:
                raise IntentParseError(f"未知 lookup_dimension: {lookup_dim}")

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
        )

    FOLLOWUP_CUES = re.compile(r"那|呢|再|换|改成|改为|只看|按|同样|这个|其中|对比|相比|相较于|较|比")
    TIME_CUES = re.compile(r"\d|月|年|季度|本周|上周|本月|上月|今年|去年|近|最近")
    DIM_WORDS = {
        "品类": "category", "类目": "category", "分类": "category",
        "商家": "seller", "店铺": "seller", "卖家": "seller",
        "商品": "product", "产品": "product",
        "省份": "state", "地区": "state", "城市": "city",
        "支付方式": "payment_type", "月份": "month",
        "延迟": "late", "准时": "late", "低分": "low_score",
    }

    def _explicit_dims(self, text: str) -> list[str]:
        hits = re.findall(
            r"按\s*(?:是否)?\s*(品类|类目|分类|商家|店铺|卖家|商品|产品|省份|地区|"
            r"城市|支付方式|月份|延迟|准时|低分)", text)
        dims = list(dict.fromkeys(self.DIM_WORDS[h] for h in hits))
        # "每家/每款/各品类" are explicit grouping requests too; treating them
        # as absent makes follow-ups silently lose the dimension.
        cue_dims = [
            (r"每家|每户|每个商家|各个商家|各商家|所有商家|全部商家", "seller"),
            (r"每款|每个商品|每件商品|各个商品|各商品|所有商品|全部商品", "product"),
            (r"每个品类|各个品类|各品类|所有品类|全部品类", "category"),
            (r"每个月|各月|每月", "month"),
            (r"每个省份|各省|所有省份", "state"),
            (r"每个城市|各城市|所有城市", "city"),
            (r"每种支付方式|各支付方式|所有支付方式", "payment_type"),
        ]
        for pattern, dim in cue_dims:
            if re.search(pattern, text) and dim not in dims:
                dims.append(dim)
        return dims

    def _postprocess_followup(self, result: ParseResult, text: str, context: dict) -> ParseResult:
        """Enforce follow-up semantics the prompt cannot guarantee.

        - "按X拆" replaces the dimension set;
        - a fresh question does not inherit the previous turn's time/dims/filters.
        """
        slots = result.slots
        explicit = self._explicit_dims(text)
        if explicit:
            slots["dimensions"] = explicit
            return result

        prev = context.get("slots") or {}
        if prev and self.FOLLOWUP_CUES.search(text) and not slots.get("filters"):
            slots["filters"] = dict(prev.get("filters") or {})
            result.notes.append("比较问题继承上一轮筛选条件")
        if not prev or self.FOLLOWUP_CUES.search(text):
            return result
        if slots.get("time") and prev.get("time") and slots["time"] == prev["time"]:
            if not self.TIME_CUES.search(text):
                slots["time"] = None
                result.notes.append("新问题未提时间，未继承上一轮时间范围")
        if slots.get("dimensions") and slots["dimensions"] == prev.get("dimensions"):
            slots["dimensions"] = []
            result.notes.append("新问题未提拆分维度，未继承上一轮维度")
        if slots.get("filters") and slots["filters"] == prev.get("filters"):
            if not any(str(v.get("value")) in text for v in slots["filters"].values()):
                slots["filters"] = {}
                result.notes.append("新问题未提筛选值，未继承上一轮筛选条件")
        return result

    # ---------- main ----------
    def parse(self, text: str, context: dict) -> ParseResult:
        if self.client.available:
            base_user = self._user.substitute(
                prev_slots=json.dumps(context.get("slots") or {}, ensure_ascii=False),
                recent_messages=json.dumps(context.get("recent_messages") or [], ensure_ascii=False),
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
                    result = self._postprocess_followup(result, text, context)
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
