"""``rr_wide``: extra columns (tenant, deposit, balance, move-in) on a legal-size landscape page.

Tenant, deposit and balance are printed only: the truth has no such fields, and extraction maps
the tenant column (used for vacancy) and ignores the other two.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from random import Random

from faker import Faker
from reportlab.lib.pagesizes import landscape, legal

from cre_screen.schemas.deal import TruthUnit
from synth.context import Style
from synth.formats import fmt_money
from synth.layouts._pdf_rr import Column, PrintRow
from synth.layouts._pdf_rr import render as render_rows

ROWS_PER_PAGE = 20
ZERO = Decimal("0.00")

# Widths sum to 936 (legal landscape minus margins).
COLUMNS: tuple[Column, ...] = (
    ("unit_id", 56, "l"),
    ("unit_type", 56, "l"),
    ("beds", 46, "r"),
    ("baths", 46, "r"),
    ("sq_ft", 56, "r"),
    ("tenant_name", 110, "l"),
    ("status", 70, "l"),
    ("in_place_rent", 66, "r"),
    ("market_rent", 66, "r"),
    ("deposit", 62, "r"),
    ("balance", 60, "r"),
    ("lease_start", 70, "l"),
    ("lease_end", 70, "l"),
    ("concession_note", 102, "l"),
)
EXTRA_HEADERS = {"deposit": ("Deposit", "Dep"), "balance": ("Balance", "Bal Due")}


def paginate(units: list[TruthUnit]) -> list[int]:
    return [i // ROWS_PER_PAGE + 1 for i in range(len(units))]


def render(
    path: Path,
    *,
    units: list[TruthUnit],
    stated_totals: dict[str, Decimal],
    as_of: date,
    style: Style,
    footnotes: list[str] | None = None,
) -> None:
    aux = Random(style.aux_seed)
    fake = Faker()
    fake.seed_instance(style.aux_seed)
    headers = dict(style.rr_headers)
    for field, options in EXTRA_HEADERS.items():
        headers[field] = aux.choice(options)

    extras: dict[str, dict[str, str]] = {}
    for unit in units:
        key = unit.unit_id + str(id(unit))
        if unit.status in ("vacant", "down"):
            tenant = "VACANT"
        elif unit.status in ("model", "employee"):
            tenant = "MODEL UNIT" if unit.status == "model" else "STAFF"
        else:
            tenant = fake.name()
        rent = unit.in_place_rent
        deposit = fmt_money(rent, style.money_style) if rent else ""
        owed = aux.choice([ZERO, ZERO, ZERO, Decimal("35.00"), Decimal("120.50")]) if rent else None
        extras[key] = {
            "tenant_name": tenant,
            "deposit": deposit,
            "balance": fmt_money(owed, style.money_style),
        }

    def extra_cell(unit: TruthUnit, field: str) -> str:
        return extras[unit.unit_id + str(id(unit))].get(field, "")

    rows = [PrintRow("unit", unit=u) for u in units]
    rows.append(
        PrintRow(
            "total",
            label="Total",
            in_place=stated_totals["in_place_rent"],
            market=stated_totals["market_rent"],
        )
    )
    render_rows(
        path,
        rows=rows,
        columns=COLUMNS,
        headers=headers,
        style=style,
        as_of=as_of,
        footnotes=footnotes or [],
        rows_per_page=ROWS_PER_PAGE,
        page_size=landscape(legal),
        extra_cell=extra_cell,
        font_size=6.8,
    )
