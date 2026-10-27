"""Type inference and value normalization for arbitrary tabular data.

The ladder is null -> boolean -> integer -> decimal -> date/datetime ->
varchar. A column is only typed when at least 98% of non-null values convert;
otherwise it stays VARCHAR and the raw text is preserved. Leading-zero
identifiers (zip codes, phone numbers, codes) are always VARCHAR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

HARD_NULL = {"", "null", "none", "n/a", "na", "nan"}
SOFT_NULL = {"-", "--", "—", "–", "无", "未知", "暂无", "/"}
BOOL_TRUE = {"true", "t", "yes", "y", "是"}
BOOL_FALSE = {"false", "f", "no", "n", "否"}
CURRENCY_CHARS = "¥￥$€£ "
PII_LABEL_RE = re.compile(
    r"姓名|名字|联系人|手机号|手机|电话|邮箱|邮件|身份证|家庭地址|收货地址|银行卡|"
    r"phone|mobile|email|address|ssn|passport",
    re.I,
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
ID_RE = re.compile(r"^\d{17}[\dXx]$")
DATE_HINT_RE = re.compile(r"日期|时间|date|time|day|dt", re.I)
ID_HINT_RE = re.compile(r"邮编|zip|电话|手机|phone|tel|编号|code|id|号码", re.I)
LEADING_ZERO_RE = re.compile(r"^0\d+$")
INT_RE = re.compile(r"^[+-]?\d+$")
SAMPLE_LIMIT = 50000
# Type probing only needs a small, evenly-spaced sample. The chosen parser is
# then run once over the whole column; the 98% / 2% rule is unchanged.
PROBE_LIMIT = 1000
PROBE_ACCEPT = 0.98
PROBE_AMBIGUOUS = 0.90

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y年%m月%d日",
    "%Y-%m-%dT%H:%M:%S",
)

TYPE_DUCKDB = {
    "integer": "BIGINT",
    "decimal": "DOUBLE",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "boolean": "BOOLEAN",
    "varchar": "VARCHAR",
    "unknown": "VARCHAR",
}


@dataclass
class InferenceResult:
    type: str
    confidence: float
    unit: str | None
    values: list[Any]
    failures: int = 0
    warnings: list[str] = field(default_factory=list)
    samples: list[Any] = field(default_factory=list)
    is_pii: bool = False
    minimum: Any = None
    maximum: Any = None


def is_hard_null(value: Any) -> bool:
    return str(value).strip().lower() in HARD_NULL


def _is_soft_null(value: Any) -> bool:
    return str(value).strip() in SOFT_NULL


def _pii(values: list[str], label: str) -> bool:
    if PII_LABEL_RE.search(label or ""):
        return True
    checked = [v for v in values if v][:500]
    if not checked:
        return False
    email = sum(1 for v in checked if EMAIL_RE.match(v))
    phone = sum(1 for v in checked if PHONE_RE.match(v))
    ident = sum(1 for v in checked if ID_RE.match(v))
    return max(email, phone, ident) / len(checked) >= 0.6


def _leading_zero_identifier(values: list[str], label: str) -> bool:
    # values arrive stripped from _raw_strings(); do not strip them again.
    checked = [v for v in values if v]
    if not checked:
        return False
    if all(LEADING_ZERO_RE.match(v) for v in checked):
        return True
    if ID_HINT_RE.search(label or "") and all(INT_RE.match(v) for v in checked):
        return any(v.startswith("0") and len(v) > 1 for v in checked)
    return False


def _clean_number_text(text: str) -> str:
    text = text.strip()
    for ch in CURRENCY_CHARS:
        text = text.replace(ch, "")
    text = text.replace(",", "").replace("，", "")
    text = text.replace("元", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    return text


def _numeric_unit(sample: list[str]) -> tuple[str | None, bool]:
    """Return (unit, percent)."""
    non_empty = [v for v in sample if v.strip()]
    if not non_empty:
        return None, False
    percent = sum(1 for v in non_empty if "%" in v or "％" in v)
    if percent / len(non_empty) >= 0.8:
        return "%", True
    if any("万" in v for v in non_empty):
        return "万", False
    if any("亿" in v for v in non_empty):
        return "亿", False
    if any(ch in v for v in non_empty for ch in "¥￥元"):
        return "元", False
    if any("$" in v for v in non_empty):
        return "$", False
    return None, False


def _parse_number(text: str, percent: bool) -> float | int | None:
    raw = text.strip()
    if not raw:
        return None
    if _is_soft_null(raw):
        return None
    is_percent = "%" in raw or "％" in raw
    scale = 1
    if "亿" in raw:
        scale = 100_000_000
    elif "万" in raw:
        scale = 10_000
    cleaned = _clean_number_text(raw)
    cleaned = cleaned.replace("%", "").replace("％", "").replace("亿", "").replace("万", "")
    if cleaned in {"", "-", "+"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    value *= scale
    if percent and is_percent:
        value /= 100.0
    elif percent and not is_percent:
        # Mixed "%" and bare numbers: treat bare numbers as percentages too.
        value /= 100.0
    if value.is_integer() and abs(value) < 2**63:
        return int(value)
    return value


def _parse_bool(text: str) -> bool | None:
    value = text.strip().lower()
    if value in BOOL_TRUE:
        return True
    if value in BOOL_FALSE:
        return False
    return None


def _parse_date(text: str, label: str) -> date | datetime | None:
    raw = text.strip()
    if not raw:
        return None
    if _is_soft_null(raw):
        return None
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    if DATE_HINT_RE.search(label or ""):
        try:
            serial = float(raw)
        except ValueError:
            return None
        if 20000 <= serial <= 80000:
            return datetime(1899, 12, 30) + timedelta(days=serial)
    return None


def _raw_strings(values: list[Any]) -> list[str]:
    return [str(v).strip() for v in values]


def _probe_sample(values: list[str], limit: int = PROBE_LIMIT) -> list[str]:
    """Evenly spaced sample; sorted columns are still represented across types."""
    if len(values) <= limit:
        return values
    step = max(1, len(values) // limit)
    return values[::step][:limit]


def _convert_column(raw_values: list[str],
                    parser: Callable[[str], Any]) -> tuple[list[Any], int]:
    """Convert the full column once and count real conversion failures."""
    converted: list[Any] = []
    failures = 0
    for raw in raw_values:
        if is_hard_null(raw):
            converted.append(None)
            continue
        value = parser(raw)
        if value is None:
            failures += 1
        converted.append(value)
    return converted, failures


def infer_column(label: str, values: list[Any], mask_pii: bool = True) -> InferenceResult:
    raw_values = _raw_strings(values)
    hard_non_null = [v for v in raw_values if not is_hard_null(v)]
    is_pii = _pii(hard_non_null, label)
    samples_source = [] if (is_pii and mask_pii) else hard_non_null
    samples = list(dict.fromkeys(samples_source))[:8]

    if not hard_non_null:
        return InferenceResult("varchar", 0.0, None,
                               [None if is_hard_null(v) else v for v in raw_values],
                               warnings=["列全部为空"], samples=[], is_pii=is_pii)

    if _leading_zero_identifier(hard_non_null, label):
        return InferenceResult(
            "varchar", 1.0, None,
            [None if is_hard_null(v) else v for v in raw_values],
            warnings=["检测到前导零标识，已保留为文本"], samples=samples, is_pii=is_pii,
        )

    sample = hard_non_null[:SAMPLE_LIMIT]
    probe = _probe_sample(hard_non_null)
    probe_size = len(probe)

    bool_values = [_parse_bool(v) for v in probe]
    bool_ok = sum(1 for v in bool_values if v is not None) / probe_size
    has_word_bool = any(v.lower() in {"true", "false", "yes", "no", "是", "否"} for v in probe)
    if bool_ok >= PROBE_ACCEPT and has_word_bool:
        converted, failures = _convert_column(raw_values, _parse_bool)
        return _finish("boolean", converted, raw_values, [], is_pii,
                       mask_pii=mask_pii, failures=failures)

    unit, percent = _numeric_unit(sample)
    probe_numbers = [_parse_number(v, percent) for v in probe]
    number_ok = sum(1 for v in probe_numbers if v is not None) / probe_size
    if number_ok >= PROBE_AMBIGUOUS:
        converted, failures = _convert_column(
            raw_values, lambda v: _parse_number(v, percent))
        failure_rate = failures / max(1, len(hard_non_null))
        if number_ok >= PROBE_ACCEPT or failure_rate <= 0.02:
            if failure_rate > 0.02:
                return _varchar(raw_values, is_pii,
                                ["全量转换失败率超过 2%，已退回文本"], mask_pii)
            integers = all(isinstance(v, int) for v in converted if v is not None)
            inferred = "integer" if integers and not percent else "decimal"
            return _finish(inferred, converted, raw_values, [], is_pii, unit=unit,
                           mask_pii=mask_pii, failures=failures)

    probe_dates = [_parse_date(v, label) for v in probe]
    date_ok = sum(1 for v in probe_dates if v is not None) / probe_size
    if date_ok >= PROBE_AMBIGUOUS:
        converted, failures = _convert_column(
            raw_values, lambda v: _parse_date(v, label))
        failure_rate = failures / max(1, len(hard_non_null))
        if date_ok >= PROBE_ACCEPT or failure_rate <= 0.02:
            if failure_rate > 0.02:
                return _varchar(raw_values, is_pii,
                                ["全量日期转换失败率超过 2%，已退回文本"], mask_pii)
            datetimes = [v for v in converted if isinstance(v, datetime)
                         and v.time() != datetime.min.time()]
            if datetimes:
                converted = [
                    (datetime.combine(v, datetime.min.time()) if isinstance(v, date)
                     and not isinstance(v, datetime) else v)
                    for v in converted
                ]
                inferred = "datetime"
            else:
                converted = [
                    v.date() if isinstance(v, datetime) else v for v in converted
                ]
                inferred = "date"
            return _finish(inferred, converted, raw_values, [], is_pii,
                           mask_pii=mask_pii, failures=failures)

    return _varchar(raw_values, is_pii, [], mask_pii)


def _varchar(raw_values: list[str], is_pii: bool,
             warnings: list[str], mask_pii: bool = True) -> InferenceResult:
    converted = [None if is_hard_null(v) else v for v in raw_values]
    samples = [] if (is_pii and mask_pii) else list(dict.fromkeys(
        v for v in raw_values if not is_hard_null(v)
    ))[:8]
    values = [v for v in converted if v is not None]
    return InferenceResult(
        "varchar", 1.0, None, converted, warnings=warnings,
        samples=samples, is_pii=is_pii,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
    )


def _finish(inferred: str, converted: list[Any], raw_values: list[str],
            warnings: list[str], is_pii: bool, unit: str | None = None,
            mask_pii: bool = True,
            failures: int | None = None) -> InferenceResult:
    non_null = [v for v in converted if v is not None]
    if failures is None:
        failures = sum(1 for raw, val in zip(raw_values, converted)
                       if not is_hard_null(raw) and val is None)
    confidence = len(non_null) / max(1, len([v for v in raw_values if not is_hard_null(v)]))
    samples: list[Any] = []
    if not (is_pii and mask_pii):
        samples = list(dict.fromkeys(
            v for v in converted if v is not None
        ))[:8]
    return InferenceResult(
        inferred, confidence, unit, converted, failures=failures,
        warnings=warnings, samples=samples, is_pii=is_pii,
        minimum=min(non_null) if non_null else None,
        maximum=max(non_null) if non_null else None,
    )


def duckdb_type(inferred: str) -> str:
    return TYPE_DUCKDB.get(inferred, "VARCHAR")
