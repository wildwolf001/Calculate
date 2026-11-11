"""Security-boundary regression tests (CSV injection, token gate helpers)."""
from __future__ import annotations

from app.core.session import _csv_cell


def test_csv_export_neutralises_formulas():
    assert _csv_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert _csv_cell("+1") == "'+1"
    assert _csv_cell("-1") == "'-1"
    assert _csv_cell("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_cell("normal") == "normal"
    assert _csv_cell(3.5) == 3.5
