"""``t12_excel``: a T-12 workbook with a title block, blank spacer rows and subtotals.

Values, not formulas, and Decimal, not float. Deductions keep their negative sign, as Excel
exports usually do.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from random import Random

from openpyxl import Workbook
from openpyxl.styles import Font

from cre_screen.schemas.deal import TruthT12Line
from synth.context import Style
from synth.formats import fmt_date
from synth.layouts._t12_common import build_blocks, subtotal_labels
from synth.xlsx_util import write_xlsx

MONEY_FORMAT = "#,##0.00;(#,##0.00)"
BOLD = Font(bold=True)


def render(
    path: Path,
    *,
    lines: list[TruthT12Line],
    month_labels: list[str],
    period_end: date,
    style: Style,
) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "T12"
    blocks = build_blocks(lines, subtotal_labels(Random(style.aux_seed)))

    ws["A1"] = style.property_name
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    ws["A2"] = "Trailing 12-Month Operating Statement"
    ws["A3"] = f"Period ending {fmt_date(period_end, style.date_style)}"

    header_row = 5
    ws.cell(header_row, 1, style.t12_label_header).font = BOLD
    for i, label in enumerate(month_labels):
        ws.cell(header_row, 2 + i, label).font = BOLD
    ws.cell(header_row, 14, "Total").font = BOLD

    row = header_row + 1

    def put(label: str, months, total, *, bold: bool = False) -> None:
        nonlocal row
        cell = ws.cell(row, 1, label)
        if bold:
            cell.font = BOLD
        for i, value in enumerate(months):
            ws.cell(row, 2 + i, value).number_format = MONEY_FORMAT
        ws.cell(row, 14, total).number_format = MONEY_FORMAT
        row += 1

    for ln in blocks.income:
        put(ln.label, ln.monthly, ln.stated_total)
    put(
        blocks.total_income.label, blocks.total_income.monthly, blocks.total_income.total, bold=True
    )
    row += 1  # blank spacer row
    for ln in blocks.expenses:
        put(ln.label, ln.monthly, ln.stated_total)
    put(
        blocks.total_expenses.label,
        blocks.total_expenses.monthly,
        blocks.total_expenses.total,
        bold=True,
    )
    row += 1
    put(blocks.noi.label, blocks.noi.monthly, blocks.noi.total, bold=True)
    if blocks.capital:
        row += 1
        for ln in blocks.capital:
            put(ln.label, ln.monthly, ln.stated_total)
    ws.cell(row + 1, 1, "Amounts in US dollars.")
    ws.column_dimensions["A"].width = 30
    write_xlsx(wb, path)
