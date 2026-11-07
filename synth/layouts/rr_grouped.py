"""``rr_grouped``: units grouped by type, a subtotal row after each group, then a grand total."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import landscape, letter

from cre_screen.schemas.deal import TruthUnit
from synth.context import Style
from synth.layouts._pdf_rr import Column, PrintRow
from synth.layouts._pdf_rr import render as render_rows

ROWS_PER_PAGE = 20
TYPE_ORDER = ("Studio", "1x1", "2x1", "2x2", "3x2")
ZERO = Decimal("0.00")

# Widths sum to 720. The first column is wide enough for "Subtotal Studio".
COLUMNS: tuple[Column, ...] = (
    ("unit_id", 78, "l"),
    ("unit_type", 55, "l"),
    ("beds", 47, "r"),
    ("baths", 47, "r"),
    ("sq_ft", 55, "r"),
    ("status", 70, "l"),
    ("in_place_rent", 70, "r"),
    ("market_rent", 70, "r"),
    ("lease_start", 70, "l"),
    ("lease_end", 70, "l"),
    ("concession_note", 88, "l"),
)


def order(units: list[TruthUnit]) -> list[TruthUnit]:
    """Printed order: by unit type, then as built. Stable, so unit ids stay ascending in a group."""
    rank = {t: i for i, t in enumerate(TYPE_ORDER)}
    return sorted(units, key=lambda u: rank.get(u.unit_type or "", len(rank)))


def print_rows(units: list[TruthUnit], stated_totals: dict[str, Decimal]) -> list[PrintRow]:
    rows: list[PrintRow] = []
    group: list[TruthUnit] = []

    def close_group() -> None:
        if group:
            rows.append(
                PrintRow(
                    "subtotal",
                    label=f"Subtotal {group[0].unit_type}",
                    in_place=sum((u.in_place_rent or ZERO for u in group), ZERO),
                    market=sum((u.market_rent or ZERO for u in group), ZERO),
                )
            )
            group.clear()

    for unit in units:
        if group and unit.unit_type != group[0].unit_type:
            close_group()
        rows.append(PrintRow("unit", unit=unit))
        group.append(unit)
    close_group()
    rows.append(
        PrintRow(
            "total",
            label="Total",
            in_place=stated_totals["in_place_rent"],
            market=stated_totals["market_rent"],
        )
    )
    return rows


def paginate(units: list[TruthUnit], stated_totals: dict[str, Decimal]) -> list[int]:
    """Printed page of each unit (subtotal and total rows take slots too)."""
    pages: list[int] = []
    for index, row in enumerate(print_rows(units, stated_totals)):
        if row.kind == "unit":
            pages.append(index // ROWS_PER_PAGE + 1)
    return pages


def render(
    path: Path,
    *,
    units: list[TruthUnit],
    stated_totals: dict[str, Decimal],
    as_of: date,
    style: Style,
    footnotes: list[str] | None = None,
) -> None:
    render_rows(
        path,
        rows=print_rows(units, stated_totals),
        columns=COLUMNS,
        headers=style.rr_headers,
        style=style,
        as_of=as_of,
        footnotes=footnotes or [],
        rows_per_page=ROWS_PER_PAGE,
        page_size=landscape(letter),
    )
