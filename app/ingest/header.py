"""Header, totals-row and column-label detection for arbitrary tables."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADER_SCAN_ROWS = 10
HEADER_THRESHOLD = 0.58

TOTAL_RE = re.compile(r"^\s*(合计|总计|小计|汇总|total|sum|grand\s+total|subtotal)\s*$",
                      re.IGNORECASE)
CURRENCY = "¥￥$€£,，%％ 元"


@dataclass
class HeaderResult:
    index: int | None
    labels: list[str]
    warnings: list[str] = field(default_factory=list)


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def _numeric(value: str) -> bool:
    text = str(value).strip()
    for ch in CURRENCY:
        text = text.replace(ch, "")
    text = text.replace("(", "-").replace(")", "")
    if text in {"", "-", "+"}:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


_DATE_RE = re.compile(
    r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$"
)


def _date_like(value: str) -> bool:
    return bool(_DATE_RE.match(str(value).strip()))


def _label_like(value: str) -> bool:
    text = str(value).strip()
    if not text or len(text) > 80:
        return False
    return not _numeric(text) and not _date_like(text)


def _ratio(values, predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if predicate(v)) / len(values)


def detect_header(rows: list[list[str]], width: int) -> HeaderResult:
    warnings: list[str] = []
    if not rows:
        return HeaderResult(index=None, labels=[], warnings=["没有可解析的数据行"])
    width = max(width, max((len(r) for r in rows[:HEADER_SCAN_ROWS]), default=0))
    best_idx, best_score = None, -1.0
    for idx, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = row[:width]
        non_empty = [c for c in cells if str(c).strip()]
        if len(non_empty) < max(1, width * 0.4):
            score = 0.0
        else:
            unique_ratio = len(set(non_empty)) / len(non_empty) if non_empty else 0.0
            label_ratio = _ratio(non_empty, _label_like)
            data_ratio = _ratio(non_empty, lambda v: _numeric(v) or _date_like(v))
            next_row = rows[idx + 1][:width] if idx + 1 < len(rows) else []
            next_non_empty = [c for c in next_row if str(c).strip()]
            homogeneity = _ratio(next_non_empty, lambda v: _numeric(v) or _date_like(v))
            score = (
                0.35 * (len(non_empty) / max(1, width))
                + 0.25 * unique_ratio
                + 0.20 * label_ratio
                + 0.20 * homogeneity
                - 0.35 * data_ratio
            )
        if score > best_score:
            best_idx, best_score = idx, score

    if best_idx is None or best_score < HEADER_THRESHOLD:
        labels = [f"column_{i + 1}" for i in range(width)]
        warnings.append(f"未识别到表头（最佳行得分 {best_score:.2f}），已生成列名")
        return HeaderResult(index=None, labels=labels, warnings=warnings)

    raw_labels = [_clean_cell(c) for c in rows[best_idx][:width]]
    labels: list[str] = []
    seen: dict[str, int] = {}
    for idx, label in enumerate(raw_labels):
        label = label or f"column_{idx + 1}"
        if label in seen:
            seen[label] += 1
            label = f"{label}_{seen[label]}"
        else:
            seen[label] = 1
        labels.append(label)
    if best_idx > 0:
        warnings.append(f"表头位于第 {best_idx + 1} 行，之前 {best_idx} 行已跳过")
    if len(set(raw_labels)) < len(raw_labels):
        warnings.append("存在重复列名，已自动加后缀")
    return HeaderResult(index=best_idx, labels=labels, warnings=warnings)


def find_totals_rows(data_rows: list[list[str]]) -> list[int]:
    """Return indices (relative to data_rows) that look like totals rows.

    They are reported, never dropped automatically.
    """
    hits: list[int] = []
    start = max(0, len(data_rows) - 8)
    for idx in range(start, len(data_rows)):
        row = [c for c in data_rows[idx] if str(c).strip()]
        if not row:
            continue
        if any(TOTAL_RE.match(str(c)) for c in row[:3]):
            hits.append(idx)
    return hits
