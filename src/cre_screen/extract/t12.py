"""RawTable -> T12Statement. Every value carries a Citation; the LLM only maps labels."""

from __future__ import annotations

import logging
from decimal import Decimal

from cre_screen.config import AppConfig
from cre_screen.errors import ParseError, T12ExtractError
from cre_screen.extract.rent_roll import PROVENANCE
from cre_screen.ingest.parse import money_was_rounded, parse_date, parse_money
from cre_screen.ingest.pdf_tables import is_month_label
from cre_screen.ingest.raw import RawCell, RawRow, RawTable
from cre_screen.mapping.coa_mapper import map_labels, subtotal_kind
from cre_screen.mapping.header_mapper import normalize_header
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.t12 import StatedSubtotal, T12Line, T12Statement

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


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


def _parse_cell(text: str, flags: list[str], warnings: list[str], where: str) -> Decimal | None:
    try:
        value = parse_money(text)
    except ParseError:
        flags.append(f"unparseable_{where}")
        warnings.append(f"{where}: could not parse {text!r}")
        return None
    if value is not None and money_was_rounded(text):
        flags.append("rounded_to_cents")
    return value


def build_t12(
    table: RawTable,
    cfg: AppConfig,
    *,
    llm: LLMClient,
    learned: dict[str, list[str]] | None = None,
) -> T12Statement:
    if not table.headers:
        raise T12ExtractError(f"no header row found in {table.source_file}")
    label_header = table.headers[0]
    month_headers = [h for h in table.headers if is_month_label(h)]
    if len(month_headers) != 12:
        raise T12ExtractError(
            f"{table.source_file}: expected 12 month columns, found {len(month_headers)}"
        )
    total_header = next((h for h in table.headers if normalize_header(h) == "total"), None)
    period_end = parse_date(month_headers[-1], end_of_month=True)
    if period_end is None:
        raise T12ExtractError(f"cannot read the last month label {month_headers[-1]!r}")

    warnings: list[str] = []
    data_rows = [
        r for r in table.rows if r.kind in ("line", "subtotal") and label_header in r.cells
    ]
    line_labels = [r.cells[label_header].text.strip() for r in data_rows if r.kind == "line"]
    mappings = map_labels(line_labels, cfg.coa, llm, learned=learned)

    lines: list[T12Line] = []
    subtotals: list[StatedSubtotal] = []
    seen: dict[str, int] = {}
    for row in data_rows:
        label_cell = row.cells[label_header]
        label = label_cell.text.strip()
        if not label:
            continue
        flags: list[str] = []
        months: list[Decimal | None] = []
        cell_citations: dict[str, Citation] = {}
        for i, header in enumerate(month_headers, start=1):
            cell = row.cells.get(header)
            if cell is None:
                months.append(None)
                continue
            value = _parse_cell(cell.text, flags, warnings, f"{label} m{i:02d}")
            months.append(value)
            cell_citations[f"m{i:02d}"] = _citation(table, row, cell)
        total: Decimal | None = None
        total_cell = row.cells.get(total_header) if total_header else None
        if total_cell is not None:
            total = _parse_cell(total_cell.text, flags, warnings, f"{label} total")
            cell_citations["total"] = _citation(table, row, total_cell)
        row_citation = _citation(table, row, label_cell)

        if row.kind == "subtotal":
            subtotals.append(
                StatedSubtotal(
                    label=label,
                    kind=subtotal_kind(label, cfg.coa) or "other",
                    monthly=months,
                    total=total,
                    citation=row_citation,
                )
            )
            continue

        mapping = mappings[label]
        filled = [m if m is not None else ZERO for m in months]
        blanks = [f"m{i:02d}" for i, m in enumerate(months, start=1) if m is None]
        if blanks:
            flags.append("blank_months:" + ",".join(blanks))
        # Deductions are stored negative. A deduction printed positive is flipped and flagged.
        bucket_cfg = cfg.coa.buckets[mapping.bucket]
        nonzero = [m for m in filled if m != 0]
        if bucket_cfg.sign == "negative" and nonzero and all(m > 0 for m in nonzero):
            filled = [-m for m in filled]
            total = None if total is None else -total
            flags.append("sign_normalized")

        seen[label] = seen.get(label, 0) + 1
        unique_label = label if seen[label] == 1 else f"{label} ({seen[label]})"
        if seen[label] > 1:
            flags.append("duplicate_label")
        lines.append(
            T12Line(
                label=unique_label,
                bucket=mapping.bucket,
                monthly=[to_cents(m) for m in filled],
                stated_total=None if total is None else to_cents(total),
                is_nonrecurring=mapping.is_nonrecurring,
                mapping_source=mapping.source,
                mapping_confidence=mapping.confidence,
                audit_flags=flags,
                citation=row_citation,
                cell_citations=cell_citations,
            )
        )

    preamble = [ln.strip() for ln in table.preamble_text.splitlines() if ln.strip()]
    return T12Statement(
        property_name=preamble[0] if preamble else None,
        period_end=period_end,
        month_labels=month_headers,
        source_file=table.source_file,
        source_format=table.source_format,
        lines=lines,
        stated_subtotals=subtotals,
        extraction_warnings=warnings,
    )
