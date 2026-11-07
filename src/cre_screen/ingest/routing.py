"""Find the rent roll and T-12 inside a PDF that holds more than just those two tables.

A user may upload a whole offering memorandum, or a one-page flyer that has a rent roll and a
T-12 on the same page. This module reads the text layer of every page, decides which pages (or,
on a shared page, which horizontal slice) hold the unit-level rent roll and the T-12 grid, and
returns them as ``Region`` lists. The table readers then see only those regions.

It is pure and deterministic: no LLM, no files written. A file that already is a plain rent roll
or T-12 gets ``None`` for that table, which means "read the whole file as before".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from cre_screen.config import RRHeadersConfig
from cre_screen.ingest.pdf_tables import (
    UNIT_ID_LIKE,
    Word,
    find_rr_header,
    find_t12_header,
    group_lines,
    is_numeric_text,
)
from cre_screen.ingest.raw import Region
from cre_screen.ingest.unwrap import unwrap_words

logger = logging.getLogger(__name__)

BROCHURE_PAGES = 4  # a file longer than this is routed even when no page holds both tables
MIN_UNIT_ROWS = 3
MIN_T12_ROWS = 3
MIN_T12_NUMBERS = 6
TITLE_REACH = 60.0  # points above a header that still count as its title
T12_TITLE = re.compile(r"t-?12|trailing|income statement|profit (&|and) loss", re.IGNORECASE)
RR_TITLE = re.compile(r"rent roll", re.IGNORECASE)
FOREIGN_TITLE = re.compile(
    r"pro[- ]?forma|property photos|investment highlights|"
    r"table of contents|disclaimer",
    re.IGNORECASE,
)
MONEY_TOKEN = re.compile(r"\$?\d[\d,]*(\.\d+)?")


@dataclass
class PageInfo:
    page: int
    height: float
    rr_top: float | None = None  # top of the rent roll header line
    rr_rows: int = 0
    loose_unit_rows: int = 0  # unit-looking rows when no header is on the page
    t12_top: float | None = None
    t12_rows: int = 0
    title: str = ""

    @property
    def foreign(self) -> bool:
        """A page that is plainly not part of a rent roll: a pro forma, photos, a cover."""
        head = self.title.lower()
        found = FOREIGN_TITLE.search(head)
        roll = head.find("rent roll")
        return found is not None and (roll == -1 or found.start() < roll)

    @property
    def has_rr(self) -> bool:
        return self.rr_top is not None and self.rr_rows >= MIN_UNIT_ROWS and not self.foreign

    @property
    def has_t12(self) -> bool:
        return self.t12_top is not None and self.t12_rows >= MIN_T12_ROWS


@dataclass
class Routing:
    pages: int
    rent_roll: list[Region] | None = None  # None: use the whole file as before
    t12: list[Region] | None = None
    has_rent_roll: bool = False
    has_t12: bool = False
    notes: list[str] = field(default_factory=list)


def _line_text(line: list[Word]) -> str:
    return " ".join(w["text"] for w in line)


def _unit_rows(lines: list[list[Word]]) -> int:
    count = 0
    for line in lines:
        if (
            len(line) >= 3
            and UNIT_ID_LIKE.match(line[0]["text"])
            and any(MONEY_TOKEN.fullmatch(w["text"].replace("$", "")) for w in line[1:])
        ):
            count += 1
    return count


def _t12_rows(lines: list[list[Word]]) -> int:
    count = 0
    for line in lines:
        label = [w for w in line if any(c.isalpha() for c in w["text"])]
        numbers = [w for w in line if is_numeric_text(w["text"])]
        if label and len(numbers) >= MIN_T12_NUMBERS:
            count += 1
    return count


def analyze_page(page_no: int, words: list[Word], height: float, cfg: RRHeadersConfig) -> PageInfo:
    info = PageInfo(page_no, height)
    lines = group_lines(unwrap_words(words))
    info.title = " ".join(_line_text(ln) for ln in lines[:6])
    found = find_rr_header(lines, cfg)
    if found is not None:
        idx = found[0]
        info.rr_top = min(w["top"] for w in lines[idx])
        info.rr_rows = _unit_rows(lines[idx + 1 :])
    else:
        info.loose_unit_rows = _unit_rows(lines)
    found12 = find_t12_header(lines)
    if found12 is not None:
        idx = found12[0]
        info.t12_top = min(w["top"] for w in lines[idx])
        info.t12_rows = _t12_rows(lines[idx + 1 :])
    return info


def _section_start(lines_by_top: list[tuple[float, str]], header_top: float, pattern) -> float:
    """Top of the title line just above a header, so the slice starts at the title."""
    best = header_top
    for top, text in lines_by_top:
        if header_top - TITLE_REACH <= top < header_top and pattern.search(text):
            best = min(best, top)
    return max(best - 1.0, 0.0)


def _page_lines(path: Path, cfg: RRHeadersConfig) -> tuple[list[PageInfo], dict[int, list]]:
    infos: list[PageInfo] = []
    titles: dict[int, list[tuple[float, str]]] = {}
    with pdfplumber.open(path) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            infos.append(analyze_page(number, words, float(page.height), cfg))
            lines = group_lines(unwrap_words(words))
            titles[number] = [(min(w["top"] for w in ln), _line_text(ln)) for ln in lines]
            close = getattr(page, "close", None)
            if close:
                close()
    return infos, titles


def route_pdf(path: Path, cfg: RRHeadersConfig) -> Routing:
    """Decide which parts of a text-layer PDF hold the rent roll and the T-12."""
    infos, titles = _page_lines(path, cfg)
    routing = Routing(pages=len(infos))
    rr_pages = [i for i in infos if i.has_rr]
    t12_pages = [i for i in infos if i.has_t12]
    routing.has_rent_roll = bool(rr_pages)
    routing.has_t12 = bool(t12_pages)
    shared = [i for i in infos if i.has_rr and i.has_t12]
    brochure = len(infos) > BROCHURE_PAGES and any(i.foreign or i.has_t12 for i in infos)
    mixed = routing.has_rent_roll and routing.has_t12
    if not (shared or brochure or mixed):
        return routing  # a plain single-table file: read it whole

    rr_regions: list[Region] = []
    t12_regions: list[Region] = []
    for info in infos:
        if info.has_rr and info.has_t12:
            assert info.rr_top is not None and info.t12_top is not None
            if info.rr_top < info.t12_top:
                split = _section_start(titles[info.page], info.t12_top, T12_TITLE)
                rr_regions.append(Region(info.page, 0.0, split))
                t12_regions.append(Region(info.page, split))
            else:
                split = _section_start(titles[info.page], info.rr_top, RR_TITLE)
                t12_regions.append(Region(info.page, 0.0, split))
                rr_regions.append(Region(info.page, split))
            routing.notes.append(f"page {info.page} holds both the rent roll and the T-12")
        elif info.has_t12:
            t12_regions.append(Region(info.page))
        elif info.has_rr:
            rr_regions.append(Region(info.page))
    # A rent roll that runs onto following pages without repeating its header.
    if rr_regions:
        last = max(r.page for r in rr_regions)
        for info in infos[last:]:
            if info.has_t12 or info.loose_unit_rows < MIN_UNIT_ROWS:
                break
            rr_regions.append(Region(info.page))
    if brochure:
        routing.notes.append(
            f"{len(infos)}-page file: rent roll on page(s) "
            f"{', '.join(str(r.page) for r in rr_regions) or 'none'}, T-12 on page(s) "
            f"{', '.join(str(r.page) for r in t12_regions) or 'none'}"
        )
    routing.rent_roll = rr_regions or None
    routing.t12 = t12_regions or None
    return routing
