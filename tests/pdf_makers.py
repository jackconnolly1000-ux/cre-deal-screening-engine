"""Builders for small PDFs that look like real-world rent rolls and T-12s.

Every table is drawn cell by cell at fixed x positions, so the text layer has the column gaps a
real table has. ``wrapped=True`` reproduces what a narrow-column renderer does: a cell's text is
split across two lines a few points above and below the row, even in the middle of a word.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

PAGE_HEIGHT = 792
FONT = 8
FRAG = 6  # points between a row's main line and its wrapped fragments

RR_COLUMNS = [("Unit", 50), ("Tenant", 100), ("Status", 200), ("Lease End", 260),
              ("Current Rent", 330), ("Last Pmt", 420)]  # fmt: skip
# header cells printed on two lines in a narrow column: (upper, lower)
RR_WRAPPED_HEADERS = {"Unit": ("Uni", "t"), "Lease End": ("Lease", "End"),
                      "Current Rent": ("Current", "Rent"), "Last Pmt": ("Last", "Pmt")}  # fmt: skip
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
T12_LABEL_X = 40
T12_FIRST_X = 150
T12_STEP = 32

Item = tuple[float, float, str]  # x, y from the top, text
Page = list[Item]


def text_pdf(path: Path, pages: list[list[str]]) -> Path:
    """A PDF whose pages are plain lines of text (no table structure)."""
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setFont("Helvetica", 10)
    for lines in pages:
        y = 740
        for line in lines:
            pdf.drawString(50, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    return path


def pages_pdf(path: Path, pages: list[Page]) -> Path:
    """A PDF from pages of positioned text items."""
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    pdf.setFont("Helvetica", FONT)
    for page in pages:
        for x, y, text in page:
            pdf.drawString(x, PAGE_HEIGHT - y, text)
        pdf.showPage()
    pdf.save()
    return path


def title(text: str, y: float = 60) -> Page:
    return [(50, y, text)]


def rent_roll_block(
    y0: float = 100, units: int = 8, *, wrapped: bool = False, first_unit: int = 101
) -> tuple[Page, float]:
    """A rent roll table starting at ``y0``; returns the items and the y below the last row."""
    items: Page = []
    y = y0
    if wrapped:
        for name, x in RR_COLUMNS:
            upper, lower = RR_WRAPPED_HEADERS.get(name, (name, ""))
            items.append((x, y, upper))
            if lower:
                items.append((x, y + FRAG, lower))
        y += FRAG + 16
    else:
        items += [(x, y, name) for name, x in RR_COLUMNS]
        y += 16
    for n in range(units):
        unit = str(first_unit + n)
        status = "Vacant" if n == units - 1 else "Active"
        tenant = "" if status == "Vacant" else f"Tenant {n}"
        rent = "$650" if status == "Active" else ""
        items += [(50, y, unit), (100, y, tenant), (200, y, status), (260, y, "MTM")]
        if rent:
            items.append((330, y, rent))
        if wrapped and status == "Active":
            items += [(420, y - FRAG, "04/03/2"), (420, y + FRAG, "6")]
            y += 30
        else:
            if status == "Active":
                items.append((420, y, "04/03/26"))
            y += 18
    return items, y


def t12_block(
    y0: float = 100,
    *,
    label_header: str | None = None,
    wrapped_total: bool = False,
    lines: int = 8,
) -> tuple[Page, float]:
    """A T-12 grid: month headers, ``lines`` line items, then a Total Income row."""
    items: Page = []
    y = y0
    if label_header:
        items.append((T12_LABEL_X, y, label_header))
    for i, month in enumerate(MONTHS):
        items.append((T12_FIRST_X + i * T12_STEP, y, month))
    total_x = T12_FIRST_X + 12 * T12_STEP
    items.append((total_x, y, "Total"))
    y += 18
    names = ["Rent Income", "Utilities", "Landscaping", "Pest Control", "Repairs",
             "Insurance", "Real Estate Taxes", "Payroll"]  # fmt: skip
    for n in range(lines):
        items.append((T12_LABEL_X, y, names[n % len(names)] + ("" if n < len(names) else f" {n}")))
        for i in range(12):
            items.append((T12_FIRST_X + i * T12_STEP, y, f"{1000 + 10 * n + i}"))
        if wrapped_total and n == 0:
            items += [(total_x, y - FRAG, "$12,34"), (total_x, y + FRAG, "5")]
        else:
            items.append((total_x, y, "12345"))
        y += 24 if wrapped_total and n == 0 else 14
    items.append((T12_LABEL_X, y, "Total Income"))
    for i in range(12):
        items.append((T12_FIRST_X + i * T12_STEP, y, f"{9000 + i}"))
    items.append((total_x, y, "108000"))
    return items, y + 14


def shifted(items: Page, dy: float) -> Page:
    return [(x, y + dy, t) for x, y, t in items]


def rent_roll_page(*, wrapped: bool = False, units: int = 8, heading: str = "RENT ROLL") -> Page:
    block, _ = rent_roll_block(100, units, wrapped=wrapped)
    return title(f"{heading} AS OF APRIL 3, 2026") + block


def t12_page(
    *, label_header: str | None = None, wrapped_total: bool = False, heading: str | None = None
) -> Page:
    block, _ = t12_block(110, label_header=label_header, wrapped_total=wrapped_total)
    head = heading or "T-12 FINANCIAL STATEMENT - JANUARY THROUGH DECEMBER 2025"
    return title(head) + block


def both_on_one_page() -> Page:
    """A flyer: the rent roll on top and the T-12 below it, on the same page."""
    roll, bottom = rent_roll_block(100, 6)
    t12, _ = t12_block(bottom + 70, lines=6)
    return (
        title("FLYER RENT ROLL AS OF APRIL 3, 2026")
        + roll
        + [(50, bottom + 40, "T-12 FINANCIAL STATEMENT - JANUARY THROUGH DECEMBER 2025")]
        + t12
    )


def filler(heading: str, *lines: str) -> Page:
    body = "This page holds marketing text and no financial table at all."
    return [(50, 60, heading), (50, 76, body), *[(50, 90 + 14 * i, t) for i, t in enumerate(lines)]]
