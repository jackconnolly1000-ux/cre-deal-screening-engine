"""Excel (.xlsx) to RawTable. Values only: formulas are never evaluated here."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter

from cre_screen.config import RRHeadersConfig
from cre_screen.errors import UnsupportedFileError
from cre_screen.ingest.pdf_tables import (
    FOOTNOTE_LIKE,
    MIN_HEADER_FIELDS,
    SUBTOTAL_LIKE,
    UNIT_ID_LIKE,
    _unique_headers,
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


def extract_excel_table(
    path: Path,
    cfg: RRHeadersConfig,
    *,
    sheet: str | None = None,
    source_file: str | None = None,
) -> RawTable:
    name = source_file or path.name
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        if sheet is not None:
            if sheet not in wb.sheetnames:
                raise UnsupportedFileError(f"sheet {sheet!r} not found in {path.name}")
            ws = wb[sheet]
        else:
            ws = wb[wb.sheetnames[0]]
        grid = [[_cell_text(c.value) for c in row] for row in ws.iter_rows(values_only=False)]
        sheet_name = ws.title
    finally:
        wb.close()

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
