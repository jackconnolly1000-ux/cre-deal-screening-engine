"""Independent re-read of a block of rent roll units, used by the gate's REEXTRACT step.

This is the I/O half of ``reconcile/reread.py`` (which stays pure). The first read clusters rows
at 3.0 pt using column boundaries taken from the header. The text-layer re-read here works from
the units' own citations instead: it looks at words within 1.5 pt of each unit-id row and takes
the money columns from the x-extents of the *data* cells, not the header. Excel re-reads the
cited cells from a fresh workbook load. A scanned page gets one more vision call.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pdfplumber

from cre_screen.errors import ParseError
from cre_screen.ingest.excel_tables import _cell_text as cell_text
from cre_screen.ingest.parse import parse_money
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.mapping.vision import page_rows, transcribe_page
from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.reconcile.reread import MONEY_FIELDS, Rereader, RereadResult, compare_money
from cre_screen.schemas.rent_roll import RentRoll, RentRollUnit

logger = logging.getLogger(__name__)

ROW_TOLERANCE = 1.5
EXTENT_PAD = 1.0
MIN_WORD_OVERLAP = 0.5


def _parse(text: str) -> Decimal | None:
    try:
        return parse_money(text)
    except ParseError:
        return None


class PdfRereader:
    def __init__(self, pdf_path: Path, rent_roll: RentRoll) -> None:
        self._path = pdf_path
        self._extents: dict[str, tuple[float, float]] = {}
        for name in MONEY_FIELDS:
            boxes = [
                u.citations[name].bbox
                for u in rent_roll.units
                if name in u.citations and u.citations[name].bbox is not None
            ]
            if boxes:
                self._extents[name] = (
                    min(b[0] for b in boxes if b) - EXTENT_PAD,
                    max(b[2] for b in boxes if b) + EXTENT_PAD,
                )

    def __call__(self, units: list[RentRollUnit]) -> RereadResult:
        words_by_page: dict[int, list[dict[str, Any]]] = {}
        values: list[dict[str, Decimal | None]] = []
        with pdfplumber.open(self._path) as pdf:
            for unit in units:
                cite = unit.citations["unit_id"]
                if cite.page is None or cite.bbox is None:
                    raise ValueError(f"unit {unit.unit_id} has no page position to re-read")
                if cite.page not in words_by_page:
                    words_by_page[cite.page] = pdf.pages[cite.page - 1].extract_words()
                row = [
                    w
                    for w in words_by_page[cite.page]
                    if abs(w["top"] - cite.bbox[1]) <= ROW_TOLERANCE
                ]
                found: dict[str, Decimal | None] = {}
                for name, (lo, hi) in self._extents.items():
                    inside = sorted(
                        (
                            w
                            for w in row
                            if (min(w["x1"], hi) - max(w["x0"], lo)) / (w["x1"] - w["x0"])
                            >= MIN_WORD_OVERLAP
                        ),
                        key=lambda w: w["x0"],
                    )
                    found[name] = _parse(" ".join(w["text"] for w in inside))
                values.append(found)
        return compare_money(units, values)


class ExcelRereader:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, units: list[RentRollUnit]) -> RereadResult:
        wb = openpyxl.load_workbook(self._path, data_only=True)
        try:
            values: list[dict[str, Decimal | None]] = []
            for unit in units:
                found: dict[str, Decimal | None] = {}
                for name in MONEY_FIELDS:
                    cite = unit.citations.get(name)
                    if cite is None or cite.sheet is None or cite.cell is None:
                        continue
                    found[name] = _parse(cell_text(wb[cite.sheet][cite.cell].value))
                values.append(found)
        finally:
            wb.close()
        return compare_money(units, values)


class VisionRereader:
    """One more vision call per page of the block, with a "read it carefully" system prompt.

    The prompt differs from the first read, so the replay cache keys it separately. "Agree" is the
    same as everywhere else: every money field of every unit equal to the cent. A unit the second
    reading cannot find counts as a disagreement.
    """

    def __init__(self, pdf_path: Path, rent_roll: RentRoll, llm: LLMClient) -> None:
        self._path = pdf_path
        self._llm = llm
        self._canonical = [c.header_text for c in rent_roll.header_map.columns]
        self._header_for = {
            c.maps_to: c.header_text for c in rent_roll.header_map.columns if c.maps_to != "ignore"
        }

    def __call__(self, units: list[RentRollUnit]) -> RereadResult:
        pages = sorted({u.citations["unit_id"].page for u in units if u.citations["unit_id"].page})
        printed: dict[str, deque[dict[str, Decimal | None]]] = defaultdict(deque)
        id_header = self._header_for["unit_id"]
        for page in pages:
            vp = transcribe_page(self._path, page, self._llm, careful=True)
            for kind, cells, _raw in page_rows(vp, self._canonical):
                if kind != "unit" or id_header not in cells:
                    continue
                values = {
                    name: _parse(cells.get(self._header_for[name], ""))
                    for name in MONEY_FIELDS
                    if name in self._header_for
                }
                printed[normalize_unit_id(cells[id_header])].append(values)
        found: list[RentRollUnit] = []
        values_found: list[dict[str, Decimal | None]] = []
        missing: list[str] = []
        for unit in units:
            queue = printed.get(normalize_unit_id(unit.unit_id))
            if queue:
                found.append(unit)
                values_found.append(queue.popleft())
            else:
                missing.append(unit.unit_id)
        result = compare_money(found, values_found)
        bad = list(dict.fromkeys([*result.disagreeing_unit_ids, *missing]))
        return RereadResult(agrees=not bad, disagreeing_unit_ids=bad)


def make_rereader(path: Path, rent_roll: RentRoll, llm: LLMClient | None = None) -> Rereader | None:
    if rent_roll.source_format == "pdf_text":
        return PdfRereader(path, rent_roll)
    if rent_roll.source_format == "xlsx":
        return ExcelRereader(path)
    if rent_roll.source_format == "pdf_scanned" and llm is not None:
        return VisionRereader(path, rent_roll, llm)
    logger.info("no re-reader for %s", rent_roll.source_format)
    return None
