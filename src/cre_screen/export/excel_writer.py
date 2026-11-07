"""Build the 7-tab underwriting workbook from a DealPackage (SPEC section 9).

Money is Decimal everywhere in ``src/``. The only float conversion is ``_num`` below, at the
moment a value is handed to openpyxl, which SPEC section 2 allows inside ``export/``.
"""

from __future__ import annotations

import io
import os
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.writer.excel import ExcelWriter

from cre_screen.errors import ExportError
from cre_screen.export import layout as L
from cre_screen.export.uw_math import Assumptions
from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import OPEX_BUCKETS, REVENUE_BUCKETS
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Finding
from cre_screen.schemas.rent_roll import RentRollUnit

BLUE = Font(color=L.BLUE)
BLACK = Font(color=L.BLACK)
BOLD = Font(bold=True)
COMMENT_SIZE = (360, 110)


def _num(value: Decimal | int | None) -> float | int | None:
    """Hand a number to openpyxl (the single place Decimal becomes float)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return float(value)


def citation_comment(citation: Citation) -> Comment:
    text = f"Source: {citation.short()}\nRaw: {citation.raw_text}\nVia: {citation.provenance}"
    comment = Comment(text, L.AUTHOR)
    comment.width, comment.height = COMMENT_SIZE
    return comment


def _note_comment(source: str, raw: str, via: str) -> Comment:
    comment = Comment(f"Source: {source}\nRaw: {raw}\nVia: {via}", L.AUTHOR)
    comment.width, comment.height = COMMENT_SIZE
    return comment


def location(citation: Citation) -> str:
    if citation.page is not None:
        return f"p.{citation.page} row {citation.row_index}"
    if citation.cell is not None:
        return f"{citation.sheet}!{citation.cell}"
    return "manual entry"


def _put(ws: Worksheet, ref: str, value: Any, *, font: Font | None = None, fmt: str | None = None):
    cell = ws[ref]
    cell.value = value
    if font is not None:
        cell.font = font
    if fmt is not None:
        cell.number_format = fmt
    return cell


def _header(ws: Worksheet, row: int, labels: list[tuple[str, str]] | list[str]) -> None:
    for index, item in enumerate(labels, start=1):
        column, label = item if isinstance(item, tuple) else (get_column_letter(index), item)
        _put(ws, f"{column}{row}", label, font=BOLD)


def _widths(ws: Worksheet, widths: dict[str, float]) -> None:
    for column, width in widths.items():
        ws.column_dimensions[column].width = width


# ---------- README ----------


def _readme(ws: Worksheet, pkg: DealPackage, synthetic: bool) -> None:
    open_blocks = len(pkg.open_blocks())
    open_warns = sum(1 for f in pkg.findings if f.severity == "warn" and f.status == "open")
    _put(ws, "A1", L.TITLE, font=Font(bold=True, size=14))
    rows = [
        ("Deal ID", pkg.deal_id),
        ("Run ID", pkg.run_id),
        ("Generated (UTC)", pkg.created_at.isoformat()),
        ("Model", pkg.model_id),
        ("Gate state", pkg.final_state),
        ("Open block findings", open_blocks),
        ("Open warn findings", open_warns),
        ("Rent roll source", pkg.rent_roll.source_file),
        ("T-12 source", pkg.t12.source_file if pkg.t12 else "none"),
    ]
    for offset, (label, value) in enumerate(rows):
        _put(ws, f"A{3 + offset}", label, font=BOLD)
        _put(ws, f"B{3 + offset}", value)
    if synthetic:
        _put(ws, "A13", L.SYNTHETIC_NOTE)
    _put(ws, "A14", L.LEGEND)
    _widths(ws, {"A": 26, "B": 44})


# ---------- Assumptions ----------


def _assumptions(ws: Worksheet, pkg: DealPackage, a: Assumptions) -> None:
    inputs: dict[str, Decimal | int | None] = {
        "AskingPrice": pkg.proforma.asking_price if pkg.proforma else None,
        "EntryCapRate": a.entry_cap_rate,
        "LTV": a.ltv,
        "InterestRate": a.interest_rate,
        "AmortYears": a.amort_years,
        "IOPeriodMonths": a.io_period_months,
        "ExpenseGrowth": a.expense_growth,
        "MgmtFeePct": a.mgmt_fee_pct,
        "ReservesPerUnit": a.reserves_per_unit,
        "AssessmentRatio": a.assessment_ratio,
        "TaxRate": a.tax_rate,
        "ReassessedTaxBump": a.reassessed_tax_bump,
    }
    for name, row in L.ASSUMPTION_NAMES:
        _put(ws, f"A{row}", name, font=BOLD)
        _put(ws, f"C{row}", L.ASSUMPTION_NOTES[name])
        fmt = L.ASSUMPTION_FORMATS[name]
        if name in L.ASSUMPTION_FORMULAS:
            _put(ws, f"B{row}", L.ASSUMPTION_FORMULAS[name], font=BLACK, fmt=fmt)
        else:
            _put(ws, f"B{row}", _num(inputs[name]), font=BLUE, fmt=fmt)

    _put(ws, f"A{L.PF_HEADER_ROW}", "Broker pro forma (annual, from proforma.yaml)", font=BOLD)
    if pkg.proforma:
        ordered = [
            b
            for b in (*REVENUE_BUCKETS, *OPEX_BUCKETS, "CAPEX_RESERVES")
            if b in pkg.proforma.lines
        ]
        if len(ordered) > L.PF_LAST - L.PF_FIRST + 1:
            raise ExportError(f"the pro forma has {len(ordered)} lines; the tab holds 20")
        for offset, bucket in enumerate(ordered):
            row = L.PF_FIRST + offset
            _put(ws, f"A{row}", bucket)
            _put(ws, f"B{row}", _num(pkg.proforma.lines[bucket]), font=BLUE, fmt=L.MONEY)  # type: ignore[index]
            _put(ws, f"C{row}", pkg.proforma.source_note)
    _widths(ws, {"A": 28, "B": 18, "C": 58})


def _define_names(wb: Workbook) -> None:
    for name, row in L.ASSUMPTION_NAMES:
        wb.defined_names[name] = DefinedName(name, attr_text=f"{L.ASSUMPTIONS}!$B${row}")
    wb.defined_names[L.BROKER_NOI_NAME] = DefinedName(
        L.BROKER_NOI_NAME, attr_text=f"{L.PROFORMA}!$D${L.ROW_NOI}"
    )
    wb.defined_names[L.T12_TAXES_NAME] = DefinedName(
        L.T12_TAXES_NAME, attr_text=f"{L.PROFORMA}!$B${L.OPEX_ROWS['REAL_ESTATE_TAXES']}"
    )


# ---------- Audit_Inputs ----------


def _audit(ws: Worksheet, pkg: DealPackage) -> None:
    _header(ws, 1, list(L.AUDIT_COLUMNS))
    row = 2

    def add(
        item: str,
        value: Any,
        comment: Comment,
        *,
        fmt: str | None,
        citation: Citation | None,
        source_file: str,
        where: str,
        raw: str,
        via: str,
    ) -> None:
        nonlocal row
        _put(ws, f"A{row}", item)
        cell = _put(ws, f"B{row}", value, font=BLUE, fmt=fmt)
        cell.comment = comment
        _put(ws, f"C{row}", source_file)
        _put(ws, f"D{row}", where)
        _put(ws, f"E{row}", raw)
        _put(ws, f"F{row}", via)
        row += 1

    def from_citation(item: str, value: Any, citation: Citation, fmt: str | None) -> None:
        add(
            item,
            value,
            citation_comment(citation),
            fmt=fmt,
            citation=citation,
            source_file=citation.source_file,
            where=location(citation),
            raw=citation.raw_text,
            via=citation.provenance,
        )

    rr = pkg.rent_roll
    for column, total in rr.stated_totals.items():  # type: ignore[attr-defined]
        from_citation(
            f"Rent roll stated total: {column}",
            _num(total),
            rr.stated_total_citations[column],
            L.MONEY,
        )  # type: ignore[index]
    for sub in rr.subtotals:
        from_citation(
            f"Rent roll {sub.scope} subtotal '{sub.label}' ({sub.column})",
            _num(sub.stated_value),
            sub.citation,
            L.MONEY,
        )
    if pkg.t12:
        for st in pkg.t12.stated_subtotals:
            if st.total is not None:
                from_citation(
                    f"T-12 stated subtotal '{st.label}'", _num(st.total), st.citation, L.MONEY
                )

    if rr.as_of_date is not None:
        add(
            "As-of date",
            rr.as_of_date,
            _note_comment(
                f"{rr.source_file} (document header or --as-of)",
                rr.as_of_date.isoformat(),
                "manual_entry" if rr.source_format == "pdf_scanned" else _via(rr.source_format),
            ),
            fmt=L.DATE,
            citation=None,
            source_file=rr.source_file,
            where="document header",
            raw=rr.as_of_date.isoformat(),
            via=_via(rr.source_format),
        )
    if pkg.t12:
        add(
            "T-12 period end",
            pkg.t12.period_end,
            _note_comment(
                f"{pkg.t12.source_file} (last month column header)",
                pkg.t12.month_labels[-1],
                _via(pkg.t12.source_format),
            ),
            fmt=L.DATE,
            citation=None,
            source_file=pkg.t12.source_file,
            where="month headers",
            raw=pkg.t12.month_labels[-1],
            via=_via(pkg.t12.source_format),
        )

    seen: set[tuple[str, str, str]] = set()
    for finding in pkg.findings:
        if finding.status not in ("open", "overridden"):
            continue
        for citation in finding.citations:
            key = (finding.check_id, citation.short(), citation.raw_text)
            if key in seen:
                continue
            seen.add(key)
            from_citation(
                f"{finding.check_id} ({finding.status}): {citation.short()}",
                citation.raw_text,
                citation,
                None,
            )
    _widths(ws, {"A": 56, "B": 18, "C": 18, "D": 20, "E": 28, "F": 14})


def _via(source_format: str) -> str:
    return {"pdf_text": "text_layer", "xlsx": "excel", "pdf_scanned": "llm_vision"}[source_format]


# ---------- Rent_Roll ----------


def _flags_by_unit(findings: list[Finding]) -> dict[str, list[str]]:
    flags: dict[str, set[str]] = defaultdict(set)
    for finding in findings:
        for unit_id in finding.unit_ids:
            flags[normalize_unit_id(unit_id)].add(finding.check_id)
    return {k: sorted(v) for k, v in flags.items()}


def _rent_roll(ws: Worksheet, pkg: DealPackage) -> None:
    units = pkg.rent_roll.units
    if len(units) > L.RR_LAST - L.RR_FIRST + 1:
        raise ExportError(f"the rent roll has {len(units)} rows; the tab holds 2,000")
    _header(ws, 1, list(L.RR_COLUMNS))
    flags = _flags_by_unit(pkg.findings)

    def money_cell(unit: RentRollUnit, field: str, ref: str, value: Decimal | None) -> None:
        cell = _put(ws, ref, _num(value), font=BLUE, fmt=L.MONEY)
        cell.comment = citation_comment(unit.citations.get(field) or unit.citations["unit_id"])

    for offset, unit in enumerate(units):
        r = L.RR_FIRST + offset
        concession = sum((c.monthly_equivalent for c in unit.concessions), Decimal("0"))
        _put(ws, f"A{r}", unit.unit_id)
        _put(ws, f"B{r}", unit.unit_type)
        _put(ws, f"C{r}", unit.beds)
        _put(ws, f"D{r}", _num(unit.baths))
        _put(ws, f"E{r}", unit.sq_ft)
        _put(ws, f"F{r}", unit.status)
        money_cell(unit, "in_place_rent", f"G{r}", unit.in_place_rent)
        money_cell(unit, "market_rent", f"H{r}", unit.market_rent)
        _put(ws, f"I{r}", unit.lease_start, fmt=L.DATE)
        _put(ws, f"J{r}", unit.lease_end, fmt=L.DATE)
        _put(ws, f"K{r}", _num(concession), font=BLUE, fmt=L.MONEY)
        _put(ws, f"L{r}", f"=G{r}-K{r}", font=BLACK, fmt=L.MONEY)
        _put(ws, f"M{r}", ", ".join(flags.get(normalize_unit_id(unit.unit_id), [])))
        _put(ws, f"N{r}", unit.citations["unit_id"].short())
    if units:
        last = L.RR_FIRST + len(units) - 1
        table = Table(displayName=L.RR_TABLE_NAME, ref=f"A1:{L.RR_LAST_COLUMN}{last}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
        ws.add_table(table)
    ws.freeze_panes = "A2"
    _widths(
        ws,
        {
            "A": 10,
            "B": 11,
            "F": 10,
            "G": 14,
            "H": 14,
            "I": 12,
            "J": 12,
            "K": 14,
            "L": 14,
            "M": 24,
            "N": 26,
        },
    )


# ---------- T12_Mapped ----------


def _t12(ws: Worksheet, pkg: DealPackage) -> None:
    lines = pkg.t12.lines if pkg.t12 else []
    if len(lines) > L.T12_LAST - L.T12_FIRST + 1:
        raise ExportError(f"the T-12 has {len(lines)} lines; the tab holds 200")
    _header(ws, 1, [(c, label) for c, label in L.T12_COLUMNS])
    labels = pkg.t12.month_labels if pkg.t12 else [f"M{i:02d}" for i in range(1, 13)]
    for column, label in zip(L.T12_MONTH_COLUMNS, labels, strict=True):
        _put(ws, f"{column}1", label, font=BOLD)
    for offset, ln in enumerate(lines):
        r = L.T12_FIRST + offset
        _put(ws, f"A{r}", ln.label)
        _put(ws, f"B{r}", ln.bucket)
        _put(ws, f"C{r}", 1 if ln.include_in_uw else 0)
        _put(ws, f"D{r}", 1 if ln.is_nonrecurring else 0)
        for column, value in zip(L.T12_MONTH_COLUMNS, ln.monthly, strict=True):
            _put(ws, f"{column}{r}", _num(value), font=BLUE, fmt=L.MONEY)
        _put(ws, f"Q{r}", f"=SUM(E{r}:P{r})", font=BLACK, fmt=L.MONEY)
        stated = _put(ws, f"R{r}", _num(ln.stated_total), font=BLUE, fmt=L.MONEY)
        stated.comment = citation_comment(ln.cell_citations.get("total") or ln.citation)
        _put(ws, f"S{r}", f"=SUM(N{r}:P{r})*4", font=BLACK, fmt=L.MONEY)
        _put(ws, f"T{r}", ln.mapping_source)
        _put(ws, f"U{r}", ln.citation.short())
    ws.freeze_panes = "C2"
    _widths(
        ws,
        {
            "A": 28,
            "B": 24,
            "T": 14,
            "U": 26,
            **{c: 12 for c in "EFGHIJKLMNOP"},
            "Q": 14,
            "R": 14,
            "S": 14,
        },
    )


# ---------- Findings ----------


def _findings(ws: Worksheet, pkg: DealPackage) -> None:
    _header(ws, 1, list(L.FINDINGS_COLUMNS))
    ordered = sorted(pkg.findings, key=lambda f: (L.SEVERITY_RANK[f.severity], f.check_id))
    for offset, f in enumerate(ordered):
        r = 2 + offset
        _put(ws, f"A{r}", f.check_id)
        _put(ws, f"B{r}", f.severity)
        _put(ws, f"C{r}", f.status)
        _put(ws, f"D{r}", f.message)
        _put(ws, f"E{r}", _num(f.delta), fmt=L.MONEY)
        _put(ws, f"F{r}", ", ".join([*f.unit_ids, *f.line_labels]))
        _put(ws, f"G{r}", "; ".join(c.short() for c in f.citations))
        _put(ws, f"H{r}", f.reviewer_note)
        ws[f"D{r}"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    _widths(ws, {"A": 20, "B": 9, "C": 11, "D": 80, "E": 14, "F": 28, "G": 50, "H": 30})


# ---------- ProForma_NOI_DSCR ----------


def _sumifs(value_range: str, bucket: str, *, include: bool = False) -> str:
    inc = f",{L.INC_RANGE},1" if include else ""
    return f'SUMIFS({value_range},{L.BKT_RANGE},"{bucket}"{inc})'


def _debt_service() -> str:
    return (
        "=MIN(IOPeriodMonths,12)*LoanAmount*InterestRate/12+(12-MIN(IOPeriodMonths,12))"
        "*-PMT(InterestRate/12,AmortYears*12,LoanAmount)"
    )


def _occupancy() -> str:
    return (
        f'=IFERROR((COUNTIFS({L.RR_STATUS_RANGE},"occupied")+COUNTIFS({L.RR_STATUS_RANGE},"notice"))'
        "/UnitCount,0)"
    )


def _loss_to_lease() -> str:
    def part(rng: str, status: str) -> str:
        return f'SUMIFS({rng},{L.RR_STATUS_RANGE},"{status}")'

    return (
        f"={part(L.RR_MARKET_RANGE, 'occupied')}+{part(L.RR_MARKET_RANGE, 'notice')}"
        f"-{part(L.RR_RENT_RANGE, 'occupied')}-{part(L.RR_RENT_RANGE, 'notice')}"
    )


def _proforma_sheet(ws: Worksheet) -> None:
    for column, label in zip("ABCDEF", L.PF_HEADER_LABELS, strict=True):
        _put(ws, f"{column}{L.PF_HEADER_ROW_NUMBER}", label, font=BOLD)
    for row, label in L.SECTION_LABEL_ROWS.items():
        _put(ws, f"A{row}", label, font=BOLD)
    for row, label in L.SUMMARY_LABELS.items():
        _put(ws, f"A{row}", label, font=BOLD if row in (L.ROW_EGI, L.ROW_NOI, L.ROW_NCF) else None)

    def formula(ref: str, text: str, row: int) -> None:
        _put(ws, ref, text, font=BLACK, fmt=L.ROW_FORMATS.get(row, L.MONEY))

    for bucket, row in {**L.REVENUE_ROWS, **L.OPEX_ROWS}.items():
        _put(ws, f"A{row}", bucket)
        formula(f"B{row}", f"={_sumifs(L.Q_RANGE, bucket)}", row)
        formula(f"C{row}", f"={_sumifs(L.S_RANGE, bucket)}", row)
        formula(f"D{row}", f'=SUMIFS({L.PFV_RANGE},{L.PFB_RANGE},"{bucket}")', row)
        if bucket in L.RENTAL_ROW_BUCKETS:
            under = f"={_sumifs(L.S_RANGE, bucket, include=True)}"
        elif bucket in L.REVENUE_ROWS:
            under = f"={_sumifs(L.Q_RANGE, bucket, include=True)}"
        elif bucket == "REAL_ESTATE_TAXES":
            under = f"=MAX({_sumifs(L.Q_RANGE, bucket, include=True)},ReassessedTax)"
        elif bucket == "MANAGEMENT_FEE":
            under = (
                f"=MAX({_sumifs(L.Q_RANGE, bucket, include=True)}*(1+ExpenseGrowth),"
                f"E{L.ROW_EGI}*MgmtFeePct)"
            )
        else:
            under = f"={_sumifs(L.Q_RANGE, bucket, include=True)}*(1+ExpenseGrowth)"
        formula(f"E{row}", under, row)

    first_rev, last_rev = min(L.REVENUE_ROWS.values()), max(L.REVENUE_ROWS.values())
    first_opex, last_opex = min(L.OPEX_ROWS.values()), max(L.OPEX_ROWS.values())
    last_rental = max(L.REVENUE_ROWS[b] for b in L.RENTAL_ROW_BUCKETS)
    gpr = L.REVENUE_ROWS["GROSS_POTENTIAL_RENT"]
    for c in L.PF_COLUMNS:
        formula(f"{c}{L.ROW_EGI}", f"=SUM({c}{first_rev}:{c}{last_rev})", L.ROW_EGI)
        formula(f"{c}{L.ROW_OPEX_TOTAL}", f"=SUM({c}{first_opex}:{c}{last_opex})", L.ROW_OPEX_TOTAL)
        formula(f"{c}{L.ROW_NOI}", f"={c}{L.ROW_EGI}-{c}{L.ROW_OPEX_TOTAL}", L.ROW_NOI)
        formula(f"{c}{L.ROW_NCF}", f"={c}{L.ROW_NOI}-{c}{L.ROW_RESERVES}", L.ROW_NCF)
        formula(f"{c}{L.ROW_PRICE}", "=PurchasePrice", L.ROW_PRICE)
        formula(
            f"{c}{L.ROW_CAP_RATE}", f"=IFERROR({c}{L.ROW_NOI}/{c}{L.ROW_PRICE},0)", L.ROW_CAP_RATE
        )
        formula(f"{c}{L.ROW_LOAN}", "=LoanAmount", L.ROW_LOAN)
        formula(f"{c}{L.ROW_DEBT_SERVICE}", _debt_service(), L.ROW_DEBT_SERVICE)
        formula(
            f"{c}{L.ROW_DSCR}", f"=IFERROR({c}{L.ROW_NCF}/{c}{L.ROW_DEBT_SERVICE},0)", L.ROW_DSCR
        )
        formula(
            f"{c}{L.ROW_DEBT_YIELD}",
            f"=IFERROR({c}{L.ROW_NOI}/{c}{L.ROW_LOAN},0)",
            L.ROW_DEBT_YIELD,
        )
        formula(
            f"{c}{L.ROW_ECON_OCC}",
            f"=IFERROR(SUM({c}{first_rev}:{c}{last_rental})/{c}{gpr},0)",
            L.ROW_ECON_OCC,
        )
    # Reserves: actual, annualized, broker, then the underwritten placeholder.
    formula(
        f"B{L.ROW_RESERVES}", f'=SUMIFS({L.Q_RANGE},{L.BKT_RANGE},"CAPEX_RESERVES")', L.ROW_RESERVES
    )
    formula(
        f"C{L.ROW_RESERVES}", f'=SUMIFS({L.S_RANGE},{L.BKT_RANGE},"CAPEX_RESERVES")', L.ROW_RESERVES
    )
    formula(
        f"D{L.ROW_RESERVES}",
        f'=SUMIFS({L.PFV_RANGE},{L.PFB_RANGE},"CAPEX_RESERVES")',
        L.ROW_RESERVES,
    )
    formula(f"E{L.ROW_RESERVES}", "=ReservesPerUnit*UnitCount", L.ROW_RESERVES)
    # Operations: computed once in E, repeated across the other columns.
    formula(f"E{L.ROW_PHYS_OCC}", _occupancy(), L.ROW_PHYS_OCC)
    formula(f"E{L.ROW_LTL}", _loss_to_lease(), L.ROW_LTL)
    for c in ("B", "C", "D"):
        formula(f"{c}{L.ROW_PHYS_OCC}", f"=E{L.ROW_PHYS_OCC}", L.ROW_PHYS_OCC)
        formula(f"{c}{L.ROW_LTL}", f"=E{L.ROW_LTL}", L.ROW_LTL)
    for row, note in L.ROW_NOTES.items():
        _put(ws, f"F{row}", note)
    ws.freeze_panes = "B3"
    _widths(ws, {"A": 30, "B": 16, "C": 16, "D": 16, "E": 16, "F": 58})


# ---------- entry point ----------


def build_workbook(
    pkg: DealPackage, *, synthetic: bool = False, assumptions: Assumptions | None = None
) -> Workbook:
    wb = Workbook()
    sheets = {name: wb.create_sheet(name) for name in L.SHEET_ORDER}
    del wb[wb.sheetnames[0]]  # openpyxl's default empty sheet
    _readme(sheets[L.README], pkg, synthetic)
    _assumptions(sheets[L.ASSUMPTIONS], pkg, assumptions or Assumptions())
    _audit(sheets[L.AUDIT], pkg)
    _rent_roll(sheets[L.RENT_ROLL], pkg)
    _t12(sheets[L.T12], pkg)
    _findings(sheets[L.FINDINGS], pkg)
    _proforma_sheet(sheets[L.PROFORMA])
    _define_names(wb)
    stamp = pkg.created_at.replace(tzinfo=None, microsecond=0)  # the file format keeps seconds
    wb.properties.creator = L.AUTHOR
    wb.properties.title = f"{pkg.deal_id} underwriting export"
    wb.properties.created = wb.properties.modified = stamp
    return wb


def write_workbook(
    pkg: DealPackage,
    path: Path,
    *,
    synthetic: bool = False,
    assumptions: Assumptions | None = None,
) -> Path:
    """Write the workbook atomically (temp file, then replace)."""
    wb = build_workbook(pkg, synthetic=synthetic, assumptions=assumptions)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # openpyxl's wb.save() stamps `modified` with the current time; ExcelWriter is what save()
    # uses minus that line, so created/modified stay the run's created_at.
    ExcelWriter(wb, ZipFile(tmp, "w", ZIP_DEFLATED, allowZip64=True)).save()
    os.replace(tmp, path)
    return path


def workbook_bytes(
    pkg: DealPackage, *, synthetic: bool = False, assumptions: Assumptions | None = None
) -> bytes:
    """The workbook as bytes, for a download button. Same content as ``write_workbook``."""
    wb = build_workbook(pkg, synthetic=synthetic, assumptions=assumptions)
    buffer = io.BytesIO()
    ExcelWriter(wb, ZipFile(buffer, "w", ZIP_DEFLATED, allowZip64=True)).save()
    return buffer.getvalue()


__all__ = ["build_workbook", "citation_comment", "workbook_bytes", "write_workbook"]
