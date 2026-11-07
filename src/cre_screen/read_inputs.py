"""Read a rent roll or a T-12 from whatever the user supplied, trying the cheap readers first.

For a text-layer PDF the order is:

1. the plain text-layer reader (what every well-formed file needs),
2. the same reader after wrapped cells are rejoined (narrow columns that split words and numbers),
3. the vision reader on the rendered pages, which is slower and costs money, so it runs only after
   the first two have failed and only on the pages routing found (or on a short file).

Each step is checked by the deterministic parsers downstream; a step that raises one of their
errors, or yields no rows, hands over to the next one. The notes of every failed step travel with
the result as extraction warnings so a reviewer can see how the file was read.

Routing finds the rent roll and T-12 pages inside a brochure, and the slices of a page that holds
both tables. Files that are plain single tables are not routed and are read whole.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from cre_screen.config import AppConfig
from cre_screen.errors import (
    CreScreenError,
    HeaderMapError,
    ParseError,
    T12ExtractError,
)
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.extract.t12 import build_t12
from cre_screen.ingest.excel_tables import extract_excel_t12_table, extract_excel_table
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table, extract_pdf_table
from cre_screen.ingest.raw import RawTable, Region
from cre_screen.ingest.router import detect_format
from cre_screen.ingest.routing import BROCHURE_PAGES, Routing, route_pdf
from cre_screen.mapping.coa_mapper import load_learned
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.mapping.vision import VisionError, vision_table
from cre_screen.schemas.rent_roll import RentRoll
from cre_screen.schemas.t12 import T12Statement

logger = logging.getLogger(__name__)

# Errors that mean "this reader could not make a table of the file": try the next reader.
RECOVERABLE = (ParseError, HeaderMapError, T12ExtractError, VisionError)
MIN_T12_MONTHS = 12


@dataclass
class Read:
    """How a file was read: the raw table, the header map's source, and notes on failed readers."""

    table: RawTable | None = None
    header_source: str = ""
    notes: list[str] = field(default_factory=list)


def sample_rows(table: RawTable) -> list[dict[str, str]]:
    units = [r for r in table.rows if r.kind == "unit"][:3]
    return [{h: c.text for h, c in r.cells.items()} for r in units]


def _pages_of(regions: list[Region] | None) -> list[int] | None:
    return None if regions is None else sorted({r.page for r in regions})


def _may_use_vision(path: Path, routing: Routing | None, regions: list[Region] | None) -> bool:
    """Vision costs money per page: allow it on routed pages or on a short file, not a brochure."""
    if regions is not None:
        return True
    pages = routing.pages if routing is not None else BROCHURE_PAGES
    return pages <= BROCHURE_PAGES


def rent_roll_routing(path: Path, cfg: AppConfig) -> Routing | None:
    """Routing for a text-layer PDF rent roll file; ``None`` for anything else."""
    if path.suffix.lower() != ".pdf" or detect_format(path) != "pdf_text":
        return None
    return route_pdf(path, cfg.rr_headers)


def _build_rent_roll(
    table: RawTable, cfg: AppConfig, llm: LLMClient, *, allow_llm: bool = True
) -> tuple[RentRoll, list[str], str]:
    if not any(r.kind == "unit" for r in table.rows):
        raise ParseError(f"no unit rows were found in {table.source_file}")
    header_map, warnings = map_headers(
        table.headers, sample_rows(table), cfg.rr_headers, llm, allow_llm=allow_llm
    )
    rent_roll = build_rent_roll(table, header_map, cfg, llm=llm)
    if not rent_roll.units:
        raise ParseError(f"no units could be built from {table.source_file}")
    return rent_roll, warnings, header_map.source


def read_rent_roll(
    path: Path,
    cfg: AppConfig,
    *,
    sheet: str | None,
    cache_dir: Path,
    llm: LLMClient,
    routing: Routing | None = None,
) -> tuple[RentRoll, Read]:
    """Rent roll file to a ``RentRoll``. Raises the last reader's error if every reader fails."""
    result = Read()
    fmt = detect_format(path)
    regions = routing.rent_roll if routing is not None else None
    if fmt == "xlsx":
        table = extract_excel_table(path, cfg.rr_headers, sheet=sheet)
        rent_roll, warnings, source = _build_rent_roll(table, cfg, llm)
        result.table, result.header_source = table, source
        return _with_notes(rent_roll, warnings, result), result

    def text_table(unwrap: bool):
        return lambda: extract_pdf_table(
            path, cfg.rr_headers, cache_dir=cache_dir, unwrap=unwrap, regions=regions
        )

    # (name, how to get the table, may the model read the headers). The cheap, deterministic
    # readers go first; the model is asked about headers only once they have all failed.
    steps: list[tuple[str, object, bool]] = []
    if fmt == "pdf_text":
        steps += [
            ("text layer", text_table(False), False),
            ("text layer with wrapped cells rejoined", text_table(True), False),
            ("text layer with the model reading the headers", text_table(False), True),
            ("rejoined text layer with the model reading the headers", text_table(True), True),
        ]
    if fmt == "pdf_scanned" or _may_use_vision(path, routing, regions):
        steps.append(
            ("page images", lambda: vision_table(path, llm, pages=_pages_of(regions)), True)
        )

    last: CreScreenError | None = None
    for name, make, allow_llm in steps:
        try:
            table = make()  # type: ignore[operator]
            rent_roll, warnings, source = _build_rent_roll(table, cfg, llm, allow_llm=allow_llm)
        except RECOVERABLE as exc:
            logger.info("rent roll: %s failed: %s", name, exc)
            result.notes.append(f"The {name} reader could not read the rent roll: {exc}")
            last = exc
            continue
        result.table, result.header_source = table, source
        return _with_notes(rent_roll, warnings, result), result
    assert last is not None
    raise last


def _with_notes(rent_roll: RentRoll, warnings: list[str], result: Read) -> RentRoll:
    extra = [*result.notes, *warnings]
    if not extra:
        return rent_roll
    return rent_roll.model_copy(
        update={"extraction_warnings": [*extra, *rent_roll.extraction_warnings]}
    )


def _check_t12_table(table: RawTable) -> None:
    months = [h for h in table.headers if _is_month(h)]
    lines = [r for r in table.rows if r.kind == "line"]
    if len(months) != MIN_T12_MONTHS or not lines:
        raise T12ExtractError(
            f"{table.source_file}: found {len(months)} month columns and {len(lines)} line items"
        )


def _is_month(text: str) -> bool:
    from cre_screen.ingest.pdf_tables import is_month_label

    return is_month_label(text)


def read_t12(
    path: Path,
    cfg: AppConfig,
    *,
    cache_dir: Path,
    llm: LLMClient,
    learned_path: Path,
    routing: Routing | None = None,
    sheet: str | None = None,
) -> tuple[T12Statement, Read]:
    """T-12 file (or the T-12 part of a routed file) to a ``T12Statement``."""
    fmt = detect_format(path)
    learned = load_learned(learned_path)
    if fmt == "xlsx":
        table = extract_excel_t12_table(path, cfg.coa, sheet=sheet)
        return build_t12(table, cfg, llm=llm, learned=learned), Read(table=table)

    regions = routing.t12 if routing is not None else None
    steps: list[tuple[str, object]] = []
    if fmt == "pdf_text":
        steps.append(
            (
                "text layer",
                lambda: extract_pdf_t12_table(path, cfg.coa, cache_dir=cache_dir, regions=regions),
            )
        )
        steps.append(
            (
                "text layer with wrapped cells rejoined",
                lambda: extract_pdf_t12_table(
                    path, cfg.coa, cache_dir=cache_dir, unwrap=True, regions=regions
                ),
            )
        )
    if fmt == "pdf_scanned" or _may_use_vision(path, routing, regions):
        steps.append(
            ("page images", lambda: vision_table(path, llm, pages=_pages_of(regions), kind="t12"))
        )

    notes: list[str] = []
    last: CreScreenError | None = None
    for name, make in steps:
        try:
            table = make()  # type: ignore[operator]
            _check_t12_table(table)
            statement = build_t12(table, cfg, llm=llm, learned=learned)
        except RECOVERABLE as exc:
            logger.info("T-12: %s failed: %s", name, exc)
            notes.append(f"The {name} reader could not read the T-12: {exc}")
            last = exc
            continue
        if notes:
            statement = statement.model_copy(
                update={"extraction_warnings": [*notes, *statement.extraction_warnings]}
            )
        return statement, Read(table=table, notes=notes)
    assert last is not None
    raise last
