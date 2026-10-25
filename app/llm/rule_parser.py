"""Deterministic Chinese question parser (offline fallback + LLM safety net).

It is deliberately narrow: metrics and dimensions are matched against the
semantic layer, values are matched against the actual warehouse catalog, and
anything it cannot bind is returned as `unresolved` so the controller asks
instead of guessing.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from app.config.settings import DIM_VALUES_PATH
from app.core.interfaces import ParseResult
from app.domain.semantic.loader import SemanticLayer

CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def normalize_text(text: str) -> str:
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    text = text.translate(trans)
    for cn, num in CN_DIGITS.items():
        text = text.replace(f"{cn}月", f"{num}月")
    return text.strip()


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _clamp(end: date, sem: SemanticLayer) -> date:
    limit = date.fromisoformat(sem.time_range[1])
    return min(end, limit)


class RuleParser:
    # order matters: more specific patterns first
    METRIC_PATTERNS: list[tuple[str, list[str]]] = [
        ("repeat_customer_count", ["复购客户", "复购人数", "复购客户数", "回头客", "复购"]),
        ("avg_order_value", ["客单价", "平均订单金额", "aov"]),
        ("low_score_rate", ["低分率", "差评率"]),
        ("avg_review_score", ["平均评分", "平均分", "满意度", "评分"]),
        ("review_count", ["评价数", "评价条数", "评论数", "多少条评价"]),
        ("delay_rate", ["延迟率", "迟到率", "延期率"]),
        ("late_order_count", ["延迟订单数", "迟到订单数", "延期订单数"]),
        ("paid_amount", ["支付金额", "实付金额", "付款金额", "实付"]),
        ("customer_count", ["购买客户数", "客户数", "买家数", "多少人"]),
        ("product_count", ["商品款数", "款数", "几款", "多少款", "单品数", "商品数"]),
        ("item_count", ["商品件数", "销量", "件数", "多少件", "几件"]),
        ("gmv", ["买了多少钱", "卖了多少钱", "花了多少钱"]),
        ("order_count", ["订单数量", "已送达订单数", "订单数", "订单量", "多少笔", "几笔"]),
        ("gmv", ["商品销售额", "销售额", "gmv", "营收", "成交额", "营业额"]),
    ]

    DIM_NOUNS = {
        "category": ["品类", "类目", "分类"],
        "seller": ["商家", "店铺", "卖家", "商户"],
        "product": ["商品", "产品", "单品"],
        "state": ["省份", "地区", "省"],
        "city": ["城市"],
        "payment_type": ["支付方式", "付款方式"],
    }
    GROUP_CUES = ["最高", "最低", "最多", "最少", "排名", "排行", "哪个", "哪些",
                  "每个", "各", "按", "分", "分别", "top", "第一", "最好", "最差"]
    ENUM_CUES = re.compile(
        r"(有哪些|有什么|都有哪些|都有什么|列出|罗列|清单|列表|包含哪些|包括哪些)"
    )
    ENUM_METRIC_CUES = re.compile(
        r"销售额|订单数|订单量|延迟率|评分|客单价|复购|排名|销量|最高|最低|最多|最少|"
        r"最好|最差|按月|趋势|占比"
    )
    RANK_DESC = ["最高", "最多", "最大", "排名", "排行", "第一", "最好", "领先", "top", "带动"]
    RANK_ASC = ["最低", "最少", "最小", "最差", "垫底"]
    UNSUPPORTED = {
        "利润": "数据没有成本字段，无法计算利润。",
        "毛利": "数据没有成本字段，无法计算毛利。",
        "成本": "数据没有成本字段。",
        "退款": "数据没有退款字段，只有订单状态（已送达/已取消/不可用/已发货）。",
        "退货": "数据没有退货记录。",
        "库存": "数据没有库存表。",
        "转化率": "数据没有曝光/访问数据，无法计算转化率。",
        "留存": "数据没有登录/活跃记录，无法计算留存。",
        "复购率": "复购率未在指标口径中定义；可改为查询“复购客户数”。",
    }

    def __init__(self, sem: SemanticLayer, dim_values_path=DIM_VALUES_PATH):
        self.sem = sem
        self.dim_values = json.loads(dim_values_path.read_text(encoding="utf-8"))
        self.product_stems: dict[str, list[str]] = self.dim_values.get("product_stems", {}) or {}
        self.product_stem_list: list[str] = sorted(self.product_stems) or self.dim_values.get("product_stem_list", [])
        self._build_value_index()

    # ---------- value index ----------
    def _build_value_index(self) -> None:
        key_dim = {
            "categories.category_name_zh": "category",
            "sellers.seller_name": "seller",
            "products.product_name": "product",
            "customers.customer_state": "state",
            "customers.customer_city": "city",
            "order_payments.payment_type": "payment_type",
        }
        index: list[tuple[str, str]] = []
        for key, dim in key_dim.items():
            for v in self.dim_values.get("values", {}).get(key, []):
                if isinstance(v, str) and len(v) >= 2:
                    index.append((v, dim))
        for key, aliases in (self.dim_values.get("aliases") or {}).items():
            dim = key_dim.get(key)
            if not dim:
                continue
            for canonical, words in aliases.items():
                for w in words:
                    if len(w) >= 2:
                        index.append((w, dim))
        # longest first so "北京数码家电商家001" beats "北京"
        self.value_index = sorted(index, key=lambda x: len(x[0]), reverse=True)

    # ---------- metric ----------
    def _metric_hits(self, text: str) -> list[tuple[str, str]]:
        low = text.lower()
        hits: list[tuple[str, str]] = []
        for mid, terms in self.METRIC_PATTERNS:
            for term in terms:
                if term.lower() in low:
                    hits.append((mid, term))
        # "客户 + 至少2笔" without the word 复购
        if re.search(r"(客户|买家|人).*(?:至少|不少于|完成).{0,6}[2二两]\s*笔", text):
            hits.append(("repeat_customer_count", "至少2笔"))
        # de-duplicate by metric, keeping the longest matched term
        best: dict[str, str] = {}
        for mid, term in hits:
            if len(term) > len(best.get(mid, "")):
                best[mid] = term
        return [(mid, term) for mid, term in best.items()]

    # ---------- time ----------
    def _latest_month(self) -> tuple[int, int]:
        return 2025, 12

    def _parse_time(self, text: str):
        latest_y, latest_m = self._latest_month()

        if re.search(r"本月|这个月", text):
            s, e = _month_bounds(latest_y, latest_m)
            return (s, _clamp(e, self.sem)), "按数据最新月份理解为本月（2025-12）"
        if re.search(r"上月|上个月", text):
            y, m = (latest_y, latest_m - 1) if latest_m > 1 else (latest_y - 1, 12)
            s, e = _month_bounds(y, m)
            return (s, _clamp(e, self.sem)), f"按数据最新月份理解 {y}-{m:02d}"
        if "今年" in text:
            return (date(2025, 1, 1), _clamp(date(2026, 1, 1), self.sem)), "今年=2025"
        if "去年" in text:
            return (date(2024, 1, 1), date(2025, 1, 1)), "去年=2024"
        m = re.search(r"(?:近|最近)\s*(\d+)\s*个月", text)
        if m:
            n = max(1, int(m.group(1)))
            end_y, end_m = latest_y, latest_m
            start_month_index = end_y * 12 + (end_m - 1) - (n - 1)
            sy, sm = divmod(start_month_index, 12)
            s, _ = _month_bounds(sy, sm + 1)
            _, e = _month_bounds(end_y, end_m)
            return (s, _clamp(e, self.sem)), f"近 {n} 个月"

        # explicit range with year(s): 2024年1月至2025年12月 / 2024年1月-6月
        m = re.search(
            r"(\d{4})\s*年\s*(\d{1,2})\s*月?\s*(?:至|到|~|—|–|-)\s*(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月",
            text,
        )
        if m:
            y1, m1 = int(m.group(1)), int(m.group(2))
            y2 = int(m.group(3)) if m.group(3) else y1
            m2 = int(m.group(4))
            s, _ = _month_bounds(y1, m1)
            _, e = _month_bounds(y2, m2)
            return (s, _clamp(e, self.sem)), f"显式区间 {y1}-{m1:02d} ~ {y2}-{m2:02d}"

        # month range without year: 1月至6月
        m = re.search(r"(\d{1,2})\s*月\s*(?:至|到|~|—|–|-)\s*(\d{1,2})\s*月", text)
        if m:
            y = latest_y
            m1, m2 = int(m.group(1)), int(m.group(2))
            s, _ = _month_bounds(y, m1)
            _, e = _month_bounds(y, m2)
            return (s, _clamp(e, self.sem)), f"未指定年份，按最近一年 {y}"

        # year + month
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", text)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            s, e = _month_bounds(y, mo)
            return (s, _clamp(e, self.sem)), f"{y}年{mo}月"
        # year only
        m = re.search(r"(20\d{2})\s*年(?!\s*\d)", text)
        if m:
            y = int(m.group(1))
            return (date(y, 1, 1), _clamp(date(y + 1, 1, 1), self.sem)), f"{y}年全年"
        # month only -> latest occurrence (policy missing_year)
        m = re.search(r"(\d{1,2})\s*月", text)
        if m:
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                y = latest_y
                s, e = _month_bounds(y, mo)
                return (s, _clamp(e, self.sem)), f"未指定年份，按数据中最近一次出现取 {y}年{mo}月"
        # quarter
        m = re.search(r"(?:第\s*([1-4])\s*季度|q([1-4]))", text.lower())
        if m:
            q = int(m.group(1) or m.group(2))
            s = date(latest_y, (q - 1) * 3 + 1, 1)
            e = _month_bounds(latest_y, q * 3)[1]
            return (s, _clamp(e, self.sem)), f"{latest_y}年第{q}季度"
        return None, None

    # ---------- dimensions ----------
    EXPLICIT_DIM = {
        "品类": "category", "类目": "category", "分类": "category",
        "商家": "seller", "店铺": "seller", "卖家": "seller", "商户": "seller",
        "商品": "product", "产品": "product", "单品": "product",
        "省份": "state", "地区": "state", "城市": "city",
        "支付方式": "payment_type", "付款方式": "payment_type", "月份": "month",
        "延迟": "late", "准时": "late", "低分": "low_score", "差评": "low_score",
    }

    def extract_context(self, text: str) -> dict:
        """Best-effort filters/time for repair hints (no metric decision)."""
        text = normalize_text(text)
        time_range, _ = self._parse_time(text)
        if isinstance(time_range, tuple) and len(time_range) == 2:
            # _parse_time returns (date, date); repair expects slot-shaped dicts.
            time_range = {"start": time_range[0].isoformat(),
                          "end": time_range[1].isoformat()}
        filters, spans = self._parse_filters(text)
        filters, _ = self._guess_product_keyword(self._mask_spans(text, spans), filters)
        return {"filters": filters, "time": time_range}

    def _lookup_dim(self, text: str, dims: list[str], prev: dict) -> str | None:
        """Detect a member-enumeration question in the offline fallback path."""
        if not self.ENUM_CUES.search(text) or self.ENUM_METRIC_CUES.search(text):
            return None
        for dim in reversed(dims):
            if dim not in {"month", "late", "low_score"}:
                return dim
        best: tuple[int, int, str] | None = None  # (noun length, position, dim)
        for dim_id, dim in self.sem.dimensions.items():
            raw = dim.raw
            if raw.get("kind") in {"time", "derived"} or not raw.get("column"):
                continue
            nouns = [raw.get("name_zh", dim_id), *(raw.get("synonyms") or [])]
            for noun in nouns:
                idx = text.rfind(str(noun))
                if idx < 0:
                    continue
                score = (len(str(noun)), idx)
                if best is None or score > (best[0], best[1]):
                    best = (score[0], score[1], dim_id)
        if best:
            return best[2]
        prev_dims = (prev or {}).get("dimensions") or []
        if (prev or {}).get("intent") == "lookup" and prev_dims:
            return prev_dims[0]
        return None

    def _parse_dims(self, text: str) -> tuple[list[str], bool]:
        # "按X拆 / 按X看" replaces the dimension set instead of extending it.
        explicit_hits = re.findall(
            r"按\s*(?:是否)?\s*(品类|类目|分类|商家|店铺|卖家|商户|商品|产品|单品|"
            r"省份|地区|城市|支付方式|付款方式|月份|延迟|准时|低分|差评)",
            text,
        )
        if explicit_hits:
            return list(dict.fromkeys(self.EXPLICIT_DIM[h] for h in explicit_hits)), True

        dims: list[str] = []
        explicit = False
        for dim, nouns in self.DIM_NOUNS.items():
            if not any(n in text for n in nouns):
                continue
            if dim == "product":
                # "商品销售额" contains 商品 but is a metric, not a grouping.
                strong = (
                    re.search(r"(哪个|哪些|按|各|每个|分)\s*(商品|产品|单品)", text)
                    or re.search(r"(商品|产品|单品)\s*(排名|排行|销量)", text)
                    or ("卖得最好" in text and ("商品" in text or "产品" in text))
                )
                if not strong:
                    continue
            if any(c in text for c in self.GROUP_CUES):
                dims.append(dim)
                explicit = True
        if re.search(r"按\s*(品类|类目|分类)", text):
            dims = [d for d in dims if d != "category"] + ["category"]
            explicit = True
        if re.search(r"按\s*(商家|店铺|卖家|商户)", text):
            dims = [d for d in dims if d != "seller"] + ["seller"]
            explicit = True
        if re.search(r"按月|每月|月度|各月|一个月一个|趋势", text):
            dims.append("month")
            explicit = True
        if ("延迟" in text and ("准时" in text or "是否" in text)) and any(c in text for c in ["按", "分别", "区分", "对比"]):
            dims.append("late")
            explicit = True
        if ("低分" in text or "差评" in text) and any(c in text for c in ["按", "分别", "区分"]):
            dims.append("low_score")
            explicit = True
        return list(dict.fromkeys(dims)), explicit

    # ---------- filters ----------
    def _parse_filters(self, text: str) -> tuple[dict, list[tuple[int, int]]]:
        spans: list[tuple[int, int, str, str]] = []
        for value, dim in self.value_index:
            start = text.find(value)
            if start >= 0:
                spans.append((start, start + len(value), dim, value))
        spans.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
        accepted: list[tuple[int, int, str, str]] = []
        for span in spans:
            if all(span[1] <= a[0] or span[0] >= a[1] for a in accepted):
                accepted.append(span)
        filters: dict[str, dict] = {}
        for _, _, dim, value in accepted:
            if dim in filters:
                existing = filters[dim]
                vals = existing["value"] if isinstance(existing["value"], list) else [existing["value"]]
                vals.append(value)
                existing["value"] = vals
                existing["op"] = "in"
            else:
                filters[dim] = {"op": "eq", "value": value}
        return filters, [(s, e) for s, e, _, _ in accepted]

    @staticmethod
    def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
        """Mask grounded values so '广州…商家004' is not read as a grouping noun."""
        if not spans:
            return text
        chars = list(text)
        for start, end in spans:
            for i in range(start, min(end, len(chars))):
                chars[i] = " "
        return "".join(chars)

    # ---------- hints ----------
    def _parse_compare(self, text: str):
        if re.search(r"同比|去年同期|较去年", text):
            return {"type": "prev_year"}
        if re.search(r"环比|较上|比上|较前|比前|增长多少|下降多少|增长率|增速|涨了|跌了|增加了|减少了", text):
            return {"type": "prev_period"}
        return None

    def _parse_rank(self, text: str, compare):
        desc = any(c in text for c in self.RANK_DESC)
        asc = any(c in text for c in self.RANK_ASC)
        limit = 100
        m = re.search(r"(?:前|top)\s*(\d+)", text.lower())
        if m:
            limit = max(1, min(int(m.group(1)), 200))
        if not desc and not asc:
            return [], limit
        field = "growth_rate" if compare and re.search(r"增长|增幅|增速", text) else "value"
        return [{"field": field, "direction": "desc" if desc else "asc"}], limit

    def _parse_having(self, text: str):
        m = re.search(r"(不少于|至少|不低于|达到|超过|大于等于|>=|≥)\s*(\d+)\s*(?:笔|单)", text)
        if not m:
            return []
        op_text, n = m.group(1), int(m.group(2))
        op = "gt" if op_text in {"超过", "大于"} else "gte"
        return [{"metric": "order_count", "op": op, "value": n}]

    # ---------- explain ----------
    def _explain(self, text: str, hits: list[tuple[str, str]]) -> ParseResult:
        if ("关联" in text or "join" in text.lower()) and ("重复" in text or "放大" in text):
            return ParseResult(
                intent_type="explain", tier="C", confidence=1.0,
                explanation=(
                    "因为订单明细表和支付表对同一订单都可能有多行：一单多件商品、一单多笔支付。"
                    "直接按 order_id 关联后求和，会把金额按行数成倍放大。正确做法是"
                    "两张表分别先按 order_id 聚合，再在订单粒度上关联。"
                ),
            )
        if hits:
            mid = max(hits, key=lambda h: len(h[1]))[0]
            m = self.sem.metric(mid)
            parts = [f"「{m.name}」的口径：{m.raw.get('definition', '未定义')}"]
            if m.raw.get("scope"):
                parts.append("统计范围：" + "，".join(f"{k} = {v}" for k, v in m.raw["scope"].items()))
            if m.raw.get("time_via"):
                parts.append(f"时间归属：{m.raw['time_via']}")
            for caveat in m.raw.get("caveats", []) or []:
                parts.append("注意：" + caveat)
            if m.raw.get("coverage_note"):
                parts.append("覆盖范围：" + m.raw["coverage_note"])
            return ParseResult(intent_type="explain", tier="C", confidence=1.0,
                               explanation="\n".join(parts))
        names = "、".join(f"{m.name}" for m in self.sem.metrics.values())
        return ParseResult(
            intent_type="explain", tier="C", confidence=0.8,
            explanation=f"当前支持这些指标：{names}。可以问某个指标的口径、时间归属或覆盖范围。",
        )


    # ---------- product family keyword ----------
    PRODUCT_STOPWORDS = [
        "销售额", "订单", "商品", "品类", "类目", "商家", "店铺", "时间", "月份",
        "多少", "一共", "总共", "每种", "每款", "各款", "分别", "买了", "卖了",
        "数据", "统计", "金额", "件数", "款数", "销量", "评分", "延迟", "客户",
        "支付", "省份", "城市", "商品数", "单品", "产品",
    ]

    def _guess_product_keyword(self, text: str, filters: dict) -> tuple[dict, str | None]:
        """Turn '键盘' into a product-family filter even though no SKU has that exact name."""
        if "product" in filters or not self.product_stem_list:
            return filters, None
        segments = re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", text)
        best: str | None = None
        for seg in segments:
            max_n = min(6, len(seg))
            for n in range(max_n, 1, -1):
                for i in range(len(seg) - n + 1):
                    cand = seg[i:i + n]
                    if any(ch.isdigit() for ch in cand):
                        continue
                    if any(w in cand for w in self.PRODUCT_STOPWORDS):
                        continue
                    matched_stems = [s for s in self.product_stem_list if cand in s]
                    if not matched_stems:
                        continue
                    # "键盘" is a suffix of 机械键盘 (good); a bare 2-char prefix
                    # shared by many families ("无线") is too generic.
                    if len(cand) < 3 and not any(
                        s.endswith(cand) or s == cand for s in matched_stems
                    ):
                        continue
                    if best is None or len(cand) > len(best):
                        best = cand
        if not best:
            return filters, None
        new_filters = dict(filters)
        new_filters["product"] = {"op": "eq", "value": best}
        return new_filters, best

    # ---------- unknown entity heuristic ----------
    ENTITY_STOPWORDS = ["月", "年", "季度", "天", "周", "已送达", "订单", "品类",
                        "类目", "商家", "商品", "店铺", "省份", "城市", "评分",
                        "客户", "自然", "全部", "所有", "总", "这", "那", "每个",
                        "各", "按", "分", "延迟", "准时", "销售额", "金额", "款数",
                        "件数", "销量", "支付", "复购", "数据", "时间"]

    def _unknown_entity(self, text: str, filters: dict) -> str | None:
        """Catch '<unknown noun>的销售额' and ask instead of silently ignoring it."""
        if filters:
            return None
        metric_words = "|".join(
            [re.escape(t) for _, terms in self.METRIC_PATTERNS for t in terms]
        )
        m = re.search(rf"([一-龥A-Za-z0-9]{{2,20}})的\s*(?:{metric_words})", text, re.I)
        if not m:
            m = re.search(
                r"^([一-龥A-Za-z0-9]{2,10}?)(?:一共|总共|买了|卖了|有多少|是多少)",
                text,
            )
        if not m:
            return None
        phrase = m.group(1).strip()
        if any(w in phrase for w in self.ENTITY_STOPWORDS):
            return None
        if any(phrase == t for _, terms in self.METRIC_PATTERNS for t in terms):
            return None
        return phrase

    # ---------- main ----------
    def parse(self, text: str, context: dict) -> ParseResult:
        text = normalize_text(text)
        prev = context.get("slots") or {}
        notes: list[str] = []

        hits = self._metric_hits(text)
        if any(w in text for w in ["口径", "定义", "什么意思", "含义", "怎么算", "如何计算", "为什么", "为何"]):
            return self._explain(text, hits)

        for word, reason in self.UNSUPPORTED.items():
            if word in text:
                return ParseResult(intent_type="unsupported", tier="A", confidence=0.9,
                                   explanation=f"{reason} 可以换一个已定义的指标再问。")

        time_range, time_note = self._parse_time(text)
        time_explicit = time_range is not None
        if time_note:
            notes.append(time_note)
        filters, filter_spans = self._parse_filters(text)
        filters, product_kw = self._guess_product_keyword(text, filters)
        dims, dims_explicit = self._parse_dims(self._mask_spans(text, filter_spans))
        if product_kw and re.search(r"每种|每款|各款|种类", text):
            if "product" not in dims:
                dims.append("product")
            dims_explicit = True
        lookup_dim = self._lookup_dim(text, dims, prev)
        if lookup_dim:
            if not filters and prev.get("filters"):
                filters = dict(prev["filters"])
            if time_range is None and prev.get("time"):
                time_range = prev["time"]
            return ParseResult(
                intent_type="lookup", tier="A", confidence=0.7,
                lookup_dim=lookup_dim,
                slots={
                    "intent": "lookup",
                    "dimensions": [lookup_dim],
                    "filters": filters,
                    "time": time_range,
                },
                notes=notes + ["规则解析：维度成员枚举"],
            )

        compare = self._parse_compare(text)
        order_by, limit = self._parse_rank(text, compare)
        having = self._parse_having(text)

        # "每种买了多少" means per-SKU units, not a SKU count.
        detail_units = bool(product_kw) and bool(re.search(r"每种|每款|各款", text)) and bool(
            re.search(r"买了多少|卖了多少|多少件|几件|多少钱", text)
        )
        if detail_units:
            hits = [h for h in hits if h[0] != "product_count"]
            mid = "gmv" if "钱" in text else "item_count"
            if not any(h[0] == mid for h in hits):
                hits.append((mid, "每种买了多少"))
        if product_kw and not hits and "多少种" in text:
            hits = [("product_count", "多少种")]

        # ---- metric selection ----
        subquestions = []
        if len(hits) > 1 and re.search(r"(和|、|以及|分别|各自)", text):
            for mid, _term in hits:
                subquestions.append({
                    "question": f"查询{self.sem.metric(mid).name}",
                    "slots": self._slots(mid, dims, time_range, filters, having,
                                         compare, order_by, limit, text, notes),
                })
            chosen = hits[0][0]
        elif hits:
            # longest matched term wins; a tie between different metrics asks
            top_len = max(len(t) for _, t in hits)
            top = [mid for mid, t in hits if len(t) == top_len]
            if len(set(top)) > 1:
                return ParseResult(
                    intent_type="query", tier="A", confidence=0.4,
                    unresolved=[text],
                    notes=[f"指标不唯一：{', '.join(sorted(set(top)))}"],
                )
            chosen = top[0]
        else:
            chosen = prev.get("metric")
            if not chosen:
                return ParseResult(intent_type="query", tier="A", confidence=0.2,
                                   unresolved=[text],
                                   notes=["没有识别出指标"])

        # ---- inheritance for follow-ups ----
        followup = not hits or bool(re.search(r"那|再|换|改成|改为|只|按|呢", text))
        if not hits and prev:
            if not dims_explicit:
                dims = list(prev.get("dimensions") or dims)
            inherited_filters = dict(prev.get("filters") or {})
            inherited_filters.update(filters)
            filters = inherited_filters
            if not time_explicit and prev.get("time"):
                time_range = prev["time"]
            if compare is None:
                compare = prev.get("compare")
            if not having:
                having = prev.get("having") or []
        if hits and followup and prev.get("metric") == chosen:
            if not dims_explicit and not filters and not time_explicit and not compare:
                dims = list(prev.get("dimensions") or [])
                filters = dict(prev.get("filters") or {})
                time_range = prev.get("time")

        unknown = self._unknown_entity(text, filters)
        if unknown and not subquestions:
            return ParseResult(
                intent_type="query", tier="A", confidence=0.5,
                slots=self._slots(chosen, dims, time_range, filters, having,
                                  compare, order_by, limit, text, notes),
                unresolved=[unknown],
                notes=notes + [f"「{unknown}」无法对应到已有的值"],
            )

        nested = None
        if chosen == "repeat_customer_count":
            m = re.search(r"(?:至少|不少于|完成)\s*(\d+)\s*笔", text)
            nested = {"entity": None, "min_orders": int(m.group(1)) if m else 2}

        slots = self._slots(chosen, dims, time_range, filters, having,
                            compare, order_by, limit, text, notes)
        if nested:
            slots["nested"] = nested

        return ParseResult(
            intent_type="query",
            tier="B" if subquestions else "A",
            slots=slots,
            subquestions=subquestions,
            notes=notes,
            confidence=0.85 if hits else 0.6,
        )

    def _slots(self, metric, dims, time_range, filters, having, compare,
               order_by, limit, text, notes) -> dict:
        slots: dict = {
            "metric": metric,
            "dimensions": list(dict.fromkeys(dims)),
            "filters": filters,
            "having": having,
            "order_by": order_by,
            "limit": limit,
            "notes": list(notes),
        }
        if time_range:
            if isinstance(time_range, dict):
                slots["time"] = time_range
            else:
                slots["time"] = {"start": str(time_range[0]), "end": str(time_range[1])}
        if compare:
            slots["compare"] = compare
        return slots
