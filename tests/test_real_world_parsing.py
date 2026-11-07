"""Small parsing rules that real documents needed: long dates, month-to-month, status words."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cre_screen.config import load_config
from cre_screen.extract.rent_roll import _leading_date
from cre_screen.ingest.parse import normalize_status, parse_date
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table
from tests.pdf_makers import pages_pdf, t12_block

CFG = load_config()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("April 3, 2026", date(2026, 4, 3)),
        ("APRIL 3, 2026", date(2026, 4, 3)),
        ("Apr 3, 2026", date(2026, 4, 3)),
        ("April 3 2026", date(2026, 4, 3)),
        ("04/03/2026", date(2026, 4, 3)),
    ],
)
def test_long_month_dates_parse(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text", ["MTM", "mtm", "Month-to-Month", "M2M", chr(0x2014), chr(0x2013), "-"]
)
def test_month_to_month_and_dashes_are_blank_dates_not_errors(text: str) -> None:
    assert parse_date(text) is None


def test_the_as_of_date_ignores_the_words_that_follow_it() -> None:
    assert _leading_date("APRIL 3, 2026") == date(2026, 4, 3)
    assert _leading_date("APRIL 3, 2026 " + chr(0xB7) + " 15 of 16 units occupied") == date(
        2026, 4, 3
    )
    assert _leading_date("4/3/2026 Unit Mix") == date(2026, 4, 3)
    assert _leading_date("sometime soon") is None


def test_active_is_an_occupied_status() -> None:
    assert normalize_status("Active", "Jackson, D.", None, CFG.rr_headers) == "occupied"


def test_a_note_under_a_t12_with_one_figure_in_it_is_not_a_line_item(tmp_path: Path) -> None:
    block, y = t12_block(110, lines=3)
    note = [
        (40, y + 10, "Pro forma after rent roll update"),
        (150, y + 10, "and removal of"),
        (182, y + 10, "$3,479"),
        (214, y + 10, "W/D capital"),
        (246, y + 10, "expenditure."),
    ]
    pdf = pages_pdf(tmp_path / "t12.pdf", [block + note])
    table = extract_pdf_t12_table(pdf, CFG.coa)
    labels = [r.cells["Line Item"].text for r in table.rows if r.kind == "line"]
    assert labels == ["Rent Income", "Utilities", "Landscaping"]
    assert any(
        r.kind == "footnote" and "Pro forma" in r.cells["Line Item"].text for r in table.rows
    )


def test_a_sparse_line_item_with_a_few_figures_is_still_a_line(tmp_path: Path) -> None:
    block, y = t12_block(110, lines=2)
    sparse = [(40, y + 10, "Court / Eviction"), (150, y + 10, "108"), (182, y + 10, "108")]
    pdf = pages_pdf(tmp_path / "t12.pdf", [block + sparse])
    table = extract_pdf_t12_table(pdf, CFG.coa)
    assert "Court / Eviction" in [r.cells["Line Item"].text for r in table.rows if r.kind == "line"]
