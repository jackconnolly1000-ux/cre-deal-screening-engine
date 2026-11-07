"""RawTable + HeaderMap -> RentRoll. Every populated value carries a Citation."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from cre_screen.config import AppConfig
from cre_screen.errors import ParseError
from cre_screen.ingest.parse import (
    money_was_rounded,
    normalize_status,
    parse_date,
    parse_decimal,
    parse_int,
    parse_money,
)
from cre_screen.ingest.raw import RawCell, RawRow, RawTable
from cre_screen.mapping.concessions import parse_concessions
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import Provenance
from cre_screen.schemas.rent_roll import HeaderMap, RentRoll, RentRollUnit, SubtotalRow

logger = logging.getLogger(__name__)

MAX_DATE_WORDS = 4


def _leading_date(text: str) -> date | None:
    """The date at the start of ``text``; text after it (a separator and more words) is ignored."""
    words = text.strip().split()
    for count in range(min(len(words), MAX_DATE_WORDS), 0, -1):
        try:
            found = parse_date(" ".join(words[:count]))
        except ParseError:
            continue
        if found is not None:
            return found
    return None


AS_OF = re.compile(r"as of[:\s]+(.+)", re.IGNORECASE)
GRAND_TOTAL = re.compile(r"grand total|total", re.IGNORECASE)
PROVENANCE: dict[str, Provenance] = {
    "pdf_text": "text_layer",
    "xlsx": "excel",
    "pdf_scanned": "llm_vision",
}


def _citation(table: RawTable, row: RawRow, cell: RawCell) -> Citation:
    return Citation(
        source_file=table.source_file,
        page=row.page,
        sheet=row.sheet,
        cell=cell.cell,
        row_index=row.row_index,
        bbox=cell.bbox,
        raw_text=cell.text,
        provenance=PROVENANCE[table.source_format],
    )


def _field_cells(row: RawRow, header_map: HeaderMap) -> dict[str, RawCell]:
    """field name -> the row's cell, for mapped columns that have text."""
    out: dict[str, RawCell] = {}
    for col in header_map.columns:
        if col.maps_to != "ignore" and col.header_text in row.cells:
            out[col.maps_to] = row.cells[col.header_text]
    return out


def _parse(
    fields: dict[str, RawCell],
    name: str,
    parser: Callable[[str], Any],
    flags: list[str],
    warnings: list[str],
    unit_id: str,
) -> Any:
    cell = fields.get(name)
    if cell is None:
        return None
    try:
        value = parser(cell.text)
    except ParseError:
        flags.append(f"unparseable_{name}")
        warnings.append(f"unit {unit_id}: could not parse {name} {cell.text!r}")
        return None
    if name in ("in_place_rent", "market_rent") and money_was_rounded(cell.text):
        flags.append("rounded_to_cents")
    return value


def _build_unit(
    table: RawTable,
    row: RawRow,
    header_map: HeaderMap,
    cfg: AppConfig,
    llm: LLMClient,
    warnings: list[str],
) -> RentRollUnit | None:
    fields = _field_cells(row, header_map)
    id_cell = fields.get("unit_id")
    if id_cell is None or not id_cell.text.strip():
        warnings.append(f"row {row.row_index}: unit row without a unit id; skipped")
        return None
    unit_id = id_cell.text.strip()
    flags: list[str] = []

    rent = _parse(fields, "in_place_rent", parse_money, flags, warnings, unit_id)
    market = _parse(fields, "market_rent", parse_money, flags, warnings, unit_id)
    beds = _parse(fields, "beds", parse_int, flags, warnings, unit_id)
    baths = _parse(fields, "baths", parse_decimal, flags, warnings, unit_id)
    sq_ft = _parse(fields, "sq_ft", parse_int, flags, warnings, unit_id)
    start = _parse(fields, "lease_start", parse_date, flags, warnings, unit_id)
    end = _parse(
        fields,
        "lease_end",
        lambda t: parse_date(t, end_of_month=True),
        flags,
        warnings,
        unit_id,
    )
    status_cell = fields.get("status")
    tenant_cell = fields.get("tenant_name")
    status = normalize_status(
        status_cell.text if status_cell else None,
        tenant_cell.text if tenant_cell else None,
        rent,
        cfg.rr_headers,
    )
    if status == "unknown":
        flags.append("unknown_status")

    citations: dict[str, Citation] = {}
    for name, cell in fields.items():
        citations[name] = _citation(table, row, cell)

    concessions = []
    note_cell = fields.get("concession_note")
    if note_cell is not None and note_cell.text.strip():
        concessions = parse_concessions(
            note_cell.text,
            in_place_rent=rent,
            citation=citations["concession_note"],
            cfg=cfg.thresholds.rr_concession,
            llm=llm,
        )
        if any(not c.grounded for c in concessions):
            flags.append("concession_ungrounded")

    unit_type = fields["unit_type"].text.strip() if "unit_type" in fields else None
    try:
        return RentRollUnit(
            unit_id=unit_id,
            unit_type=unit_type or None,
            beds=beds,
            baths=baths,
            sq_ft=sq_ft,
            status=status,
            in_place_rent=rent,
            market_rent=market,
            lease_start=start,
            lease_end=end,
            concessions=concessions,
            audit_flags=flags,
            citations=citations,
        )
    except ValidationError as exc:
        warnings.append(f"unit {unit_id}: rejected by schema: {exc.errors()[0]['msg']}")
        return None


def build_rent_roll(
    table: RawTable, header_map: HeaderMap, cfg: AppConfig, *, llm: LLMClient
) -> RentRoll:
    warnings: list[str] = []
    units: list[RentRollUnit] = []
    # (row, unit ids that precede it since the previous subtotal row)
    pending_subtotals: list[tuple[RawRow, list[str], list[str | None]]] = []
    block_ids: list[str] = []
    block_types: list[str | None] = []

    for row in table.rows:
        if row.kind == "unit":
            unit = _build_unit(table, row, header_map, cfg, llm, warnings)
            if unit is not None:
                units.append(unit)
                block_ids.append(unit.unit_id)
                block_types.append(unit.unit_type)
        elif row.kind == "subtotal":
            pending_subtotals.append((row, block_ids, block_types))
            block_ids, block_types = [], []

    subtotals: list[SubtotalRow] = []
    stated_totals: dict[Any, Decimal] = {}
    stated_citations: dict[Any, Citation] = {}
    all_ids = [u.unit_id for u in units]
    for index, (row, covers, types) in enumerate(pending_subtotals):
        fields = _field_cells(row, header_map)
        label = next(iter(row.cells.values())).text
        is_last = index == len(pending_subtotals) - 1
        if is_last and GRAND_TOTAL.search(label):
            scope, covered = "grand_total", all_ids
        elif types and len(set(types)) == 1 and types[0] is not None:
            scope, covered = "unit_type", covers
        else:
            scope, covered = "page", covers
        for column in ("in_place_rent", "market_rent"):
            cell = fields.get(column)
            if cell is None:
                continue
            try:
                value = parse_money(cell.text)
            except ParseError:
                warnings.append(f"subtotal {label!r}: could not parse {column} {cell.text!r}")
                continue
            if value is None:
                continue
            citation = _citation(table, row, cell)
            subtotals.append(
                SubtotalRow(
                    label=label,
                    column=column,  # type: ignore[arg-type]
                    scope=scope,  # type: ignore[arg-type]
                    stated_value=value,
                    covers_unit_ids=list(covered),
                    citation=citation,
                )
            )
            if scope == "grand_total":
                stated_totals[column] = value
                stated_citations[column] = citation

    lines = [ln.strip() for ln in table.preamble_text.splitlines() if ln.strip()]
    property_name = lines[0] if lines else None
    as_of = None
    match = AS_OF.search(table.preamble_text)
    if match:
        as_of = _leading_date(match.group(1))
        if as_of is None:
            warnings.append(f"could not parse as-of date {match.group(1).strip()!r}")
    if as_of is None and "no as-of date found" not in warnings:
        warnings.append("no as-of date found")

    return RentRoll(
        property_name=property_name,
        as_of_date=as_of,
        source_file=table.source_file,
        source_format=table.source_format,
        header_map=header_map,
        units=units,
        stated_totals=stated_totals,
        stated_total_citations=stated_citations,
        subtotals=subtotals,
        extraction_warnings=warnings,
    )
