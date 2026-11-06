"""``t12_grid``: one row per account, 12 month columns and a total column."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from reportlab import rl_config
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

from cre_screen.schemas.deal import TruthT12Line
from synth.context import Style
from synth.formats import fmt_date, fmt_money

LEFT = 24
LABEL_W = 138
MONTH_W = 45
TOTAL_W = 56
ROW_HEIGHT = 14


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

    c.setFont("Helvetica-Bold", 13)
    c.drawString(LEFT, page_h - 44, style.property_name)
    c.setFont("Helvetica", 10)
    c.drawString(LEFT, page_h - 60, "Trailing 12-Month Operating Statement")
    c.setFont("Helvetica", 9)
    c.drawString(LEFT, page_h - 74, f"Period ending {fmt_date(period_end, style.date_style)}")

    header_y = page_h - 100
    money = "commas" if style.money_style == "dollar_commas" else style.money_style
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(LEFT + 2, header_y, style.t12_label_header)
    for i, label in enumerate(month_labels):
        right = LEFT + LABEL_W + MONTH_W * (i + 1) - 2
        c.drawRightString(right, header_y, label)
    total_right = LEFT + LABEL_W + MONTH_W * 12 + TOTAL_W - 2
    c.drawRightString(total_right, header_y, "Total")
    c.line(LEFT, header_y - 5, total_right + 2, header_y - 5)

    c.setFont("Helvetica", 6.5)
    y = header_y - 18
    previous_section = None
    for line in lines:
        section = "revenue" if line.true_bucket in _REVENUE else "other"
        if previous_section is not None and section != previous_section:
            y -= ROW_HEIGHT  # blank spacer row between income and expense accounts
        previous_section = section
        c.drawString(LEFT + 2, y, line.label)
        for i, value in enumerate(line.monthly):
            right = LEFT + LABEL_W + MONTH_W * (i + 1) - 2
            c.drawRightString(
                right, y, fmt_money(value, money, neg=style.t12_neg, zero_dash=style.zero_dash)
            )
        c.drawRightString(
            total_right,
            y,
            fmt_money(line.stated_total, money, neg=style.t12_neg, zero_dash=style.zero_dash),
        )
        y -= ROW_HEIGHT

    c.setFont("Helvetica", 6.5)
    c.drawString(
        LEFT, y - 14, "Amounts in US dollars. Income deductions are shown as negative amounts."
    )
    c.drawCentredString(page_w / 2, 24, "Page 1 of 1")
    c.showPage()
    c.save()
    os.replace(tmp, path)


_REVENUE = (
    "GROSS_POTENTIAL_RENT",
    "LOSS_TO_LEASE",
    "VACANCY_LOSS",
    "CONCESSIONS",
    "BAD_DEBT",
    "UTILITY_REIMBURSEMENT",
    "OTHER_INCOME",
)
