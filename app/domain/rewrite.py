"""Deterministic pre-parse normalization ("rewrite").

No model call: it only cleans surface noise and recognizes a few intents that
must not reach the LLM (greetings, "list all categories"). Semantic completion
of elliptical follow-ups stays in parse_intent, which already sees the
previous slots.

The result is emitted as `query.normalized`, so every change is auditable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

POLITE_PREFIX = re.compile(
    r"^(?:请问|麻烦你?|帮我|帮忙|我想知道|我想问|问一下|查一下|查询一下|看一下|看下|给我|请)\s*"
)
POLITE_SUFFIX = re.compile(r"(?:[吗啊呀吧]|谢谢|多谢)+[？?。！!]*$")
SMALLTALK = re.compile(
    r"^(?:你好|您好|hi|hello|嗨|哈喽|在吗|在不在|早上好|下午好|晚上好|谢谢|多谢|再见|拜拜)"
    r"(?:啊|呀|啦|哈|哦|嘛|呢)*[!！。,.，~～\s]*$",
    re.IGNORECASE,
)
SMALLTALK_PREFIX = re.compile(r"^(?:你好|您好|hi|hello|嗨|哈喽)", re.IGNORECASE)
FOLLOWUP = re.compile(r"那|呢|再|换|改成|改为|只看|按|同样|这个|其中|对比|相比")

DIM_WORDS = [
    ("category", ["品类", "类目", "分类", "商品种类", "商品类别"]),
    ("seller", ["商家", "店铺", "卖家", "商户"]),
    ("product", ["商品", "产品", "单品"]),
    ("state", ["省份", "地区"]),
    ("city", ["城市"]),
    ("payment_type", ["支付方式", "付款方式"]),
]
LOOKUP_VERBS = r"(?:有哪些|有什么|都有哪些|都有什么|一共有哪些|共有哪些|列出|罗列|清单|列表|有多少种|有多少个)"
LOOKUP_EXCLUDE = re.compile(
    r"销售额|订单数|订单量|延迟率|评分|客单价|复购|排名|最高|最低|最多|最少|按月|趋势|占比"
)


@dataclass
class RewriteResult:
    original: str
    normalized: str
    changes: list[str] = field(default_factory=list)
    kind: str | None = None          # smalltalk | lookup | None
    lookup_dim: str | None = None
    followup: bool = False


def _normalize(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    out = text.strip()
    if not out:
        return out, changes
    trans = str.maketrans("０１２３４５６７８９（）", "0123456789()")
    converted = out.translate(trans)
    if converted != out:
        changes.append("全角字符转半角")
        out = converted
    collapsed = re.sub(r"\s+", " ", out)
    if collapsed != out:
        changes.append("压缩空白")
        out = collapsed
    stripped = POLITE_PREFIX.sub("", out)
    if stripped != out:
        changes.append("去除礼貌用语")
        out = stripped.strip()
    stripped = POLITE_SUFFIX.sub("", out).strip()
    if stripped != out:
        changes.append("去除语气词")
        out = stripped
    return out, changes


def _lookup_dim(text: str) -> str | None:
    if LOOKUP_EXCLUDE.search(text):
        return None
    for dim, words in DIM_WORDS:
        for word in words:
            if word not in text:
                continue
            if re.search(rf"{re.escape(word)}\s*{LOOKUP_VERBS}", text):
                return dim
            if re.search(rf"{LOOKUP_VERBS}\s*(?:的)?\s*{re.escape(word)}", text):
                return dim
            if word == "商品种类" and re.search(r"商品种类", text):
                return dim
    return None


def rewrite(text: str) -> RewriteResult:
    normalized, changes = _normalize(text)
    result = RewriteResult(
        original=text,
        normalized=normalized or text.strip(),
        changes=changes,
        followup=bool(FOLLOWUP.search(normalized)),
    )
    if SMALLTALK.match(normalized) or (
        len(normalized) <= 12 and SMALLTALK_PREFIX.match(normalized)
    ):
        result.kind = "smalltalk"
        return result
    dim = _lookup_dim(normalized)
    if dim:
        result.kind = "lookup"
        result.lookup_dim = dim
    return result
