"""Excel (.xlsx) to RawTable. Values only: formulas are never evaluated here."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter

from cre_screen.config import CoaConfig, RRHeadersConfig
from cre_screen.errors import UnsupportedFileError
from cre_screen.ingest.pdf_tables import (
    FOOTNOTE_LIKE,
    MIN_HEADER_FIELDS,
    MIN_T12_MONTH_HEADERS,
    SUBTOTAL_LIKE,
    UNIT_ID_LIKE,
    _t12_classifier,
    _unique_headers,
    is_month_label,
)
from cre_screen.ingest.raw import RawCell, RawRow, RawTable, RowKind
from cre_screen.mapping.header_mapper import match_headers

HEADER_SEARCH_ROWS = 30


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # openpyxl returns floats for numeric cells; go through str so 1250.1 stays 1250.1.
        exact = Decimal(str(value))
        return str(int(exact)) if exact == exact.to_integral_value() else format(exact, "f")
    return str(value).strip()


def _load_values(path: Path, sheet: str | None) -> tuple[list[list[Any]], str]:
    """Raw cell values of one sheet (first sheet unless named), and the sheet title."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        if sheet is not None:
            if sheet not in wb.sheetnames:
                raise UnsupportedFileError(f"sheet {sheet!r} not found in {path.name}")
            ws = wb[sheet]
        else:
            ws = wb[wb.sheetnames[0]]
        return [[c.value for c in row] for row in ws.iter_rows(values_only=False)], ws.title
    finally:
        wb.close()


def extract_excel_table(
    path: Path,
    cfg: RRHeadersConfig,
    *,
    sheet: str | None = None,
    source_file: str | None = None,
) -> RawTable:
    name = source_file or path.name
    values, sheet_name = _load_values(path, sheet)
    grid = [[_cell_text(v) for v in row] for row in values]

    header_row: int | None = None
    best = 0
    for r, row in enumerate(grid[:HEADER_SEARCH_ROWS]):
        score = len(match_headers([t for t in row if t], cfg))
        if score >= MIN_HEADER_FIELDS and score > best:
            best, header_row = score, r
    if header_row is None:
        raise UnsupportedFileError(f"no header row found in the first {HEADER_SEARCH_ROWS} rows")

    header_cols = [c for c, text in enumerate(grid[header_row]) if text]
    headers = _unique_headers([grid[header_row][c] for c in header_cols])
    preamble = "\n".join(" ".join(t for t in row if t) for row in grid[:header_row] if any(row))

    rows: list[RawRow] = []
    for r in range(header_row + 1, len(grid)):
        row = grid[r]
        cells: dict[str, RawCell] = {}
        for header, c in zip(headers, header_cols, strict=True):
            text = row[c] if c < len(row) else ""
            if text:
                cells[header] = RawCell(text, None, f"{get_column_letter(c + 1)}{r + 1}")
        kind: RowKind
        if not cells:
            kind = "blank"
        else:
            first_header = next(h for h in headers if h in cells)
            first = cells[first_header].text
            if SUBTOTAL_LIKE.search(first):
                kind = "subtotal"
            elif len(cells) == 1 and FOOTNOTE_LIKE.match(first):
                kind = "footnote"
            elif first_header == headers[0] and UNIT_ID_LIKE.match(first):
                kind = "unit"
            else:
                kind = "footnote"
        rows.append(RawRow(None, sheet_name, len(rows), kind, cells))

    return RawTable(
        source_file=name,
        source_format="xlsx",
        headers=headers,
        rows=rows,
        page_count=1,
        header_texts_by_page={},
        preamble_text=preamble,
    )


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _header_text(value: Any) -> str:
    """Header cells that hold real dates become 'Oct-25' labels; everything else is text."""
    if isinstance(value, (datetime, date)):
        return f"{_MONTHS[value.month - 1]}-{value.year % 100:02d}"
    return _cell_text(value)


def extract_excel_t12_table(
    path: Path,
    coa: CoaConfig,
    *,
    sheet: str | None = None,
    source_file: str | None = None,
) -> RawTable:
    """T-12 workbook to RawTable: header = the row of month labels, rows = line or subtotal."""
    values, sheet_name = _load_values(path, sheet)
    header_row: int | None = None
    for r, row in enumerate(values[:HEADER_SEARCH_ROWS]):
        if sum(1 for v in row if v is not None and is_month_label(_header_text(v))) >= (
            MIN_T12_MONTH_HEADERS
        ):
            header_row = r
            break
    if header_row is None:
        raise UnsupportedFileError(
            f"no month-label header row in the first {HEADER_SEARCH_ROWS} rows"
        )

    header_cols = [c for c, v in enumerate(values[header_row]) if v is not None and _cell_text(v)]
    headers = _unique_headers([_header_text(values[header_row][c]) for c in header_cols])
    grid = [[_cell_text(v) for v in row] for row in values]
    preamble = "\n".join(" ".join(t for t in row if t) for row in grid[:header_row] if any(row))
    classify = _t12_classifier(coa)

    rows: list[RawRow] = []
    for r in range(header_row + 1, len(grid)):
        row = grid[r]
        cells: dict[str, RawCell] = {}
        for header, c in zip(headers, header_cols, strict=True):
            text = row[c] if c < len(row) else ""
            if text:
                cells[header] = RawCell(text, None, f"{get_column_letter(c + 1)}{r + 1}")
        rows.append(RawRow(None, sheet_name, len(rows), classify(cells, headers, False), cells))

    return RawTable(
        source_file=source_file or path.name,
        source_format="xlsx",
        headers=headers,
        rows=rows,
        page_count=1,
        header_texts_by_page={},
        preamble_text=preamble,
    )
