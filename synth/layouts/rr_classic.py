"""``rr_classic``: one table, header repeated on every page, grand total on the last page."""

from __future__ import annotations

import math
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab import rl_config
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

from cre_screen.schemas.deal import TruthUnit
from synth.context import Style
from synth.formats import STATUS_TEXT, fmt_date, fmt_money

ROWS_PER_PAGE = 20
ROW_HEIGHT = 18
LEFT = 36

# (field, width in points, alignment). Widths sum to 720 (landscape letter minus margins).
COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("unit_id", 62, "l"),
    ("unit_type", 55, "l"),
    ("beds", 47, "r"),
    ("baths", 47, "r"),
    ("sq_ft", 55, "r"),
    ("status", 70, "l"),
    ("in_place_rent", 70, "r"),
    ("market_rent", 70, "r"),
    ("lease_start", 70, "l"),
    ("lease_end", 70, "l"),
    ("concession_note", 104, "l"),
)


def page_count(n_rows: int) -> int:
    return max(1, math.ceil(n_rows / ROWS_PER_PAGE))


def _cell(unit: TruthUnit, field: str, style: Style) -> str:
    if field in ("in_place_rent", "market_rent"):
        return fmt_money(getattr(unit, field), style.money_style)
    if field in ("lease_start", "lease_end"):
        return fmt_date(getattr(unit, field), style.date_style)
    if field == "status":
        return STATUS_TEXT[style.status_style][unit.status]
    if field == "baths":
        return "" if unit.baths is None else f"{unit.baths:f}"
    value = getattr(unit, field)
    return "" if value is None else str(value)


def _put(c: canvas.Canvas, text: str, x: float, width: int, align: str, y: float) -> None:
    if not text:
        return
    if align == "r":
        c.drawRightString(x + width - 3, y, text)
    else:
        c.drawString(
            x + 6, y, text
        )  # >= 9 pt to the next cell: wider than the 6 pt header-merge rule


def render(
    path: Path,
    *,
    units: list[TruthUnit],
    stated_totals: dict[str, Decimal],
    as_of: date,
    style: Style,
) -> None:
    rl_config.invariant = 1
    page_w, page_h = landscape(letter)
    tmp = path.with_name(path.name + ".tmp")
    c = canvas.Canvas(str(tmp), pagesize=(page_w, page_h), invariant=1)
    c.setTitle(f"{style.property_name} Rent Roll")
    c.setAuthor("")
    n_pages = page_count(len(units))
    header_y = page_h - 100

    for page in range(n_pages):
        if page == 0:
            c.setFont("Helvetica-Bold", 13)
            c.drawString(LEFT, page_h - 44, style.property_name)
            c.setFont("Helvetica", 10)
            c.drawString(LEFT, page_h - 60, "Rent Roll")
            c.setFont("Helvetica", 9)
            c.drawString(LEFT, page_h - 74, f"As of {fmt_date(as_of, style.date_style)}")
        else:
            c.setFont("Helvetica", 9)
            c.drawString(LEFT, page_h - 44, f"{style.property_name} - Rent Roll (continued)")

        c.setFont("Helvetica-Bold", 7)
        x = LEFT
        for field, width, align in COLUMNS:
            _put(c, style.rr_headers[field], x, width, align, header_y)
            x += width
        c.line(LEFT, header_y - 5, LEFT + 720, header_y - 5)

        c.setFont("Helvetica", 7)
        y = header_y - 20
        for unit in units[page * ROWS_PER_PAGE : (page + 1) * ROWS_PER_PAGE]:
            x = LEFT
            for field, width, align in COLUMNS:
                _put(c, _cell(unit, field, style), x, width, align, y)
                x += width
            y -= ROW_HEIGHT

        if page == n_pages - 1:
            c.line(LEFT, y + 10, LEFT + 720, y + 10)
            y -= 6
            c.setFont("Helvetica-Bold", 7)
            x = LEFT
            for field, width, align in COLUMNS:
                if field == "unit_id":
                    _put(c, "Total", x, width, align, y)
                elif field in stated_totals:
                    _put(c, fmt_money(stated_totals[field], style.money_style), x, width, align, y)
                x += width
            c.setFont("Helvetica", 6.5)
            c.drawString(LEFT, y - 24, "Rents are scheduled monthly amounts in US dollars.")
            c.drawString(LEFT, y - 34, "Vacant units show no lease dates or in-place rent.")
        c.setFont("Helvetica", 7)
        c.drawCentredString(page_w / 2, 24, f"Page {page + 1} of {n_pages}")
        c.showPage()

    c.save()
    os.replace(tmp, path)
