"""``t12_sections``: Income and Expenses sections with subtotals and NOI.

Deductions (loss to lease, vacancy, concessions, bad debt) are printed as positive numbers under
a "Less:" label, the way many operating statements show them. Extraction has to notice and flip
the sign (audit flag ``sign_normalized``); the truth keeps the chart-of-accounts sign.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from random import Random

from reportlab import rl_config
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

from cre_screen.schemas.deal import TruthT12Line
from synth.context import Style
from synth.formats import fmt_date, fmt_money
from synth.layouts._t12_common import DEDUCTION_BUCKETS, build_blocks, subtotal_labels

LEFT = 24
LABEL_W = 138
MONTH_W = 45
TOTAL_W = 56
ROW_HEIGHT = 12


def render(
    path: Path,
    *,
    lines: list[TruthT12Line],
    month_labels: list[str],
    period_end: date,
    style: Style,
) -> None:
    rl_config.invariant = 1
    page_w, page_h = landscape(letter)
    tmp = path.with_name(path.name + ".tmp")
    c = canvas.Canvas(str(tmp), pagesize=(page_w, page_h), invariant=1)
    c.setTitle(f"{style.property_name} T-12")
    c.setAuthor("")
    money = "commas" if style.money_style == "dollar_commas" else style.money_style
    blocks = build_blocks(lines, subtotal_labels(Random(style.aux_seed)))

    def text(value: Decimal) -> str:
        return fmt_money(value, money, neg=style.t12_neg, zero_dash=style.zero_dash)

    c.setFont("Helvetica-Bold", 13)
    c.drawString(LEFT, page_h - 44, style.property_name)
    c.setFont("Helvetica", 10)
    c.drawString(LEFT, page_h - 60, "Operating Statement - Trailing 12 Months")
    c.setFont("Helvetica", 9)
    c.drawString(LEFT, page_h - 74, f"Period ending {fmt_date(period_end, style.date_style)}")

    header_y = page_h - 100
    total_right = LEFT + LABEL_W + MONTH_W * 12 + TOTAL_W - 2
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(LEFT + 2, header_y, style.t12_label_header)
    for i, label in enumerate(month_labels):
        c.drawRightString(LEFT + LABEL_W + MONTH_W * (i + 1) - 2, header_y, label)
    c.drawRightString(total_right, header_y, "Total")
    c.line(LEFT, header_y - 5, total_right + 2, header_y - 5)

    y = header_y - 16

    def label_row(label: str, *, bold: bool = True) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 6.5)
        c.drawString(LEFT + 2, y, label)
        y -= ROW_HEIGHT

    def amount_row(
        label: str, months: list[Decimal], total: Decimal, *, bold: bool = False
    ) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 6.5)
        if bold:
            c.line(LEFT + LABEL_W, y + 8, total_right + 2, y + 8)
        c.drawString(LEFT + 2 + (0 if bold else 6), y, label)
        for i, value in enumerate(months):
            c.drawRightString(LEFT + LABEL_W + MONTH_W * (i + 1) - 2, y, text(value))
        c.drawRightString(total_right, y, text(total))
        y -= ROW_HEIGHT

    def line_row(ln: TruthT12Line) -> None:
        flip = ln.true_bucket in DEDUCTION_BUCKETS
        sign = -1 if flip else 1
        amount_row(ln.label, [sign * m for m in ln.monthly], sign * ln.stated_total)

    label_row("Income")
    printed_less = False
    for ln in blocks.income:
        if ln.true_bucket in DEDUCTION_BUCKETS and not printed_less:
            label_row("Less:", bold=False)
            printed_less = True
        line_row(ln)
    amount_row(
        blocks.total_income.label, blocks.total_income.monthly, blocks.total_income.total, bold=True
    )
    y -= ROW_HEIGHT // 2
    label_row("Expenses")
    for ln in blocks.expenses:
        line_row(ln)
    amount_row(
        blocks.total_expenses.label,
        blocks.total_expenses.monthly,
        blocks.total_expenses.total,
        bold=True,
    )
    y -= ROW_HEIGHT // 2
    amount_row(blocks.noi.label, blocks.noi.monthly, blocks.noi.total, bold=True)
    if blocks.capital:
        y -= ROW_HEIGHT // 2
        label_row("Below the line")
        for ln in blocks.capital:
            line_row(ln)

    c.setFont("Helvetica", 6.5)
    c.drawString(
        LEFT, y - 12, "Amounts in US dollars. Deductions under Less: are shown as positive amounts."
    )
    c.drawCentredString(page_w / 2, 24, "Page 1 of 1")
    c.showPage()
    c.save()
    os.replace(tmp, path)
