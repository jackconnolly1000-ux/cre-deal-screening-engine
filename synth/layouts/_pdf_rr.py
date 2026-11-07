"""A generic paged rent roll table for the grouped and wide layouts."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab import rl_config
from reportlab.pdfgen import canvas

from cre_screen.schemas.deal import TruthUnit
from synth.context import Style
from synth.formats import STATUS_TEXT, fmt_date, fmt_money

ROW_HEIGHT = 18
LEFT = 36
Column = tuple[str, int, str]  # (field, width in points, "l" or "r")


@dataclass(frozen=True)
class PrintRow:
    kind: str  # "unit", "subtotal" or "total"
    unit: TruthUnit | None = None
    label: str = ""
    in_place: Decimal | None = None
    market: Decimal | None = None


def page_count(n_rows: int, rows_per_page: int) -> int:
    return max(1, math.ceil(n_rows / rows_per_page))


def _unit_cell(unit: TruthUnit, field: str, style: Style) -> str:
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
        c.drawString(x + 6, y, text)


def render(
    path: Path,
    *,
    rows: list[PrintRow],
    columns: tuple[Column, ...],
    headers: dict[str, str],
    style: Style,
    as_of: date,
    footnotes: list[str],
    rows_per_page: int,
    page_size: tuple[float, float],
    extra_cell: Callable[[TruthUnit, str], str] | None = None,
    font_size: float = 7.0,
) -> None:
    rl_config.invariant = 1
    page_w, page_h = page_size
    table_w = sum(w for _f, w, _a in columns)
    tmp = path.with_name(path.name + ".tmp")
    c = canvas.Canvas(str(tmp), pagesize=page_size, invariant=1)
    c.setTitle(f"{style.property_name} Rent Roll")
    c.setAuthor("")
    n_pages = page_count(len(rows), rows_per_page)
    header_y = page_h - 100
    y = header_y

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

        c.setFont("Helvetica-Bold", font_size)
        x = LEFT
        for field, width, align in columns:
            _put(c, headers[field], x, width, align, header_y)
            x += width
        c.line(LEFT, header_y - 5, LEFT + table_w, header_y - 5)

        y = header_y - 20
        for row in rows[page * rows_per_page : (page + 1) * rows_per_page]:
            bold = row.kind != "unit"
            c.setFont("Helvetica-Bold" if bold else "Helvetica", font_size)
            x = LEFT
            for field, width, align in columns:
                if row.kind == "unit":
                    assert row.unit is not None
                    if extra_cell is not None and (extra := extra_cell(row.unit, field)):
                        text = extra
                    else:
                        text = (
                            ""
                            if field in ("tenant_name", "deposit", "balance")
                            else _unit_cell(row.unit, field, style)
                        )
                elif field == columns[0][0]:
                    text = row.label
                elif field == "in_place_rent":
                    text = fmt_money(row.in_place, style.money_style)
                elif field == "market_rent":
                    text = fmt_money(row.market, style.money_style)
                else:
                    text = ""
                _put(c, text, x, width, align, y)
                x += width
            y -= ROW_HEIGHT

        if page == n_pages - 1:
            c.setFont("Helvetica", 6.5)
            lines = [
                "Rents are scheduled monthly amounts in US dollars.",
                "Vacant units show no lease dates or in-place rent.",
                *footnotes,
            ]
            for k, text in enumerate(lines):
                c.drawString(LEFT, y - 8 - 10 * k, text)
        c.setFont("Helvetica", 7)
        c.drawCentredString(page_w / 2, 24, f"Page {page + 1} of {n_pages}")
        c.showPage()

    c.save()
    os.replace(tmp, path)
