"""``rr_excel``: a rent roll workbook with a merged title block, a merged header and blank spacers.

Values only (never formulas), money as Decimal, dates as real dates. There are no pages, so the
truth has no page numbers for these deals.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from cre_screen.schemas.deal import TruthUnit
from synth.context import Style
from synth.formats import STATUS_TEXT, fmt_date
from synth.xlsx_util import write_xlsx

FIELDS = (
    "unit_id",
    "unit_type",
    "beds",
    "baths",
    "sq_ft",
    "status",
    "in_place_rent",
    "market_rent",
    "lease_start",
    "lease_end",
    "concession_note",
)
SPACER_EVERY = 12
MONEY_FORMAT = "#,##0.00"
DATE_FORMATS = {"us_slash": "mm/dd/yyyy", "iso": "yyyy-mm-dd", "day_mon": "dd-mmm-yyyy"}
BOLD = Font(bold=True)


def paginate(units: list[TruthUnit]) -> list[None]:
    return [None] * len(units)


def render(
    path: Path,
    *,
    units: list[TruthUnit],
    stated_totals: dict[str, Decimal],
    as_of: date,
    style: Style,
    footnotes: list[str] | None = None,
) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Rent Roll"
    ws["A1"] = style.property_name
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    ws["A2"] = "Rent Roll"
    ws["A3"] = f"As of {fmt_date(as_of, style.date_style)}"
    # row 4 stays blank: a spacer between the title block and the table

    header_row = 5
    for col, field in enumerate(FIELDS, start=1):
        ws.cell(header_row, col, style.rr_headers[field]).font = BOLD
    notes_col = FIELDS.index("concession_note") + 1
    ws.merge_cells(
        start_row=header_row, start_column=notes_col, end_row=header_row, end_column=notes_col + 1
    )

    date_format = DATE_FORMATS[style.date_style]
    row = header_row + 1
    for count, unit in enumerate(units, start=1):
        values = {
            "unit_id": unit.unit_id,
            "unit_type": unit.unit_type,
            "beds": unit.beds,
            "baths": unit.baths,
            "sq_ft": unit.sq_ft,
            "status": STATUS_TEXT[style.status_style][unit.status],
            "in_place_rent": unit.in_place_rent,
            "market_rent": unit.market_rent,
            "lease_start": unit.lease_start,
            "lease_end": unit.lease_end,
            "concession_note": unit.concession_note,
        }
        for col, field in enumerate(FIELDS, start=1):
            cell = ws.cell(row, col, values[field])
            if field in ("in_place_rent", "market_rent"):
                cell.number_format = MONEY_FORMAT
            elif field in ("lease_start", "lease_end"):
                cell.number_format = date_format
        row += 1
        if count % SPACER_EVERY == 0 and count != len(units):
            row += 1  # blank spacer row inside the table

    ws.cell(row, 1, "Total").font = BOLD
    for field in ("in_place_rent", "market_rent"):
        cell = ws.cell(row, FIELDS.index(field) + 1, stated_totals[field])
        cell.font = BOLD
        cell.number_format = MONEY_FORMAT
    row += 2
    ws.cell(row, 1, "Rents are scheduled monthly amounts in US dollars.")
    for k, text in enumerate(footnotes or [], start=1):
        ws.cell(row + k, 1, text)
    ws.column_dimensions["A"].width = 12
    write_xlsx(wb, path)
