"""Everything the review UI does that is not drawing widgets, so it can be tested directly."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from cre_screen.config import ThresholdsConfig
from cre_screen.errors import ParseError
from cre_screen.export.uw_math import compute_uw
from cre_screen.ingest import render
from cre_screen.ingest.parse import money_was_rounded, parse_money
from cre_screen.review import BUCKETS, edit_target, remap_target
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Finding, Override

DEV_DEALS = range(1, 15)
HELDOUT_DEALS = range(15, 21)  # listed only when CRE_SHOW_HELDOUT=1
SEVERITY_RANK = {"block": 0, "warn": 1, "info": 2}
RENDER_DPI = 110
Box = tuple[float, float, float, float]
DealKind = Literal["synthetic", "real", "upload"]
UPLOAD_NAMES = {"rent_roll": "rent_roll", "t12": "t12", "proforma": "proforma"}


@dataclass(frozen=True)
class DealRef:
    name: str
    path: Path
    kind: DealKind


def show_heldout() -> bool:
    return os.environ.get("CRE_SHOW_HELDOUT") == "1"


def list_deals(data_dir: Path, real_dir: Path, *, heldout: bool | None = None) -> list[DealRef]:
    """Dev synthetic deals (and held-out ones only when asked), then real deals.

    Synthetic deals are named by number, never discovered by globbing, so a held-out folder is not
    even looked at unless the owner switched it on.
    """
    include_heldout = show_heldout() if heldout is None else heldout
    numbers = [*DEV_DEALS, *(HELDOUT_DEALS if include_heldout else ())]
    deals = [
        DealRef(f"deal_{n:03d}", data_dir / f"deal_{n:03d}", "synthetic")
        for n in numbers
        if (data_dir / f"deal_{n:03d}").is_dir()
    ]
    if real_dir.is_dir():
        deals += [
            DealRef(p.name, p, "real")
            for p in sorted(real_dir.iterdir())
            if p.is_dir() and any(p.glob("rent_roll.*"))
        ]
    return deals


def folder_hash(path: Path) -> str:
    """Hash of the files the pipeline reads, so edited documents never hit a stale cache."""
    digest = hashlib.sha256()
    for stem in ("rent_roll", "t12"):
        for candidate in sorted(path.glob(f"{stem}.*")):
            digest.update(candidate.name.encode("utf-8"))
            digest.update(candidate.read_bytes())
    proforma = path / "proforma.yaml"
    if proforma.exists():
        digest.update(proforma.read_bytes())
    return digest.hexdigest()


def overrides_hash(overrides: Sequence[Override]) -> str:
    blob = "\n".join(o.model_dump_json() for o in overrides)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def save_upload(
    files: dict[str, tuple[str, bytes]], base: Path, now: datetime | None = None
) -> Path:
    """Write uploaded documents to ``<base>/<timestamp>/`` under the names the pipeline expects.

    ``files`` maps a role (rent_roll, t12, proforma) to (original name, bytes).
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S")
    folder = base / stamp
    folder.mkdir(parents=True, exist_ok=True)
    for role, (original, data) in files.items():
        if role not in UPLOAD_NAMES:
            raise ValueError(f"unknown upload role {role!r}")
        suffix = ".yaml" if role == "proforma" else Path(original).suffix.lower()
        if role != "proforma" and suffix not in (".pdf", ".xlsx"):
            raise ValueError(f"{original}: only .pdf and .xlsx files can be uploaded")
        (folder / f"{UPLOAD_NAMES[role]}{suffix}").write_bytes(data)
    return folder


# ---------- findings ----------


def sorted_findings(pkg: DealPackage) -> list[Finding]:
    return sorted(pkg.findings, key=lambda f: (SEVERITY_RANK[f.severity], f.check_id))


def findings_rows(findings: Iterable[Finding]) -> list[dict[str, str]]:
    return [
        {
            "Check": f.check_id,
            "Severity": f.severity,
            "Status": f.status,
            "Message": f.message,
            "Delta": "" if f.delta is None else f"{f.delta:,.2f}",
            "Units / lines": ", ".join([*f.unit_ids, *f.line_labels]),
            "First citation": f.citations[0].short() if f.citations else "",
        }
        for f in findings
    ]


def finding_label(finding: Finding) -> str:
    text = finding.message if len(finding.message) <= 70 else finding.message[:67] + "..."
    return f"[{finding.severity}] {finding.check_id}: {text}"


def reviewable(pkg: DealPackage) -> list[Finding]:
    """Open findings a reviewer can clear or override, worst first."""
    return [f for f in sorted_findings(pkg) if f.status == "open"]


def counts(pkg: DealPackage) -> dict[str, int]:
    return {
        "block": len(pkg.open_blocks()),
        "warn": sum(1 for f in pkg.findings if f.severity == "warn" and f.status == "open"),
        "info": sum(1 for f in pkg.findings if f.severity == "info"),
    }


# ---------- focus and the audit view ----------


def citation_dict(citation: Citation) -> dict[str, Any]:
    return {
        "source_file": citation.source_file,
        "page": citation.page,
        "sheet": citation.sheet,
        "cell": citation.cell,
        "row_index": citation.row_index,
        "bbox": list(citation.bbox) if citation.bbox else None,
        "raw_text": citation.raw_text,
        "provenance": citation.provenance,
        "short": citation.short(),
    }


def focus_of(finding: Finding) -> dict[str, Any] | None:
    """A finding's first citation: where the audit view should look."""
    return citation_dict(finding.citations[0]) if finding.citations else None


def default_focus(pkg: DealPackage) -> dict[str, Any] | None:
    for finding in sorted_findings(pkg):
        focus = focus_of(finding)
        if focus is not None:
            return focus
    return None


def _all_citations(pkg: DealPackage) -> Iterable[Citation]:
    rr = pkg.rent_roll
    for unit in rr.units:
        yield from unit.citations.values()
        for concession in unit.concessions:
            yield concession.citation
    yield from rr.stated_total_citations.values()
    for sub in rr.subtotals:
        yield sub.citation
    if pkg.t12:
        for line in pkg.t12.lines:
            yield line.citation
            yield from line.cell_citations.values()
        for subtotal in pkg.t12.stated_subtotals:
            yield subtotal.citation
    for finding in pkg.findings:
        yield from finding.citations


def boxes_on_page(pkg: DealPackage, source_file: str, page: int) -> list[Box]:
    """Every distinct citation box on one page of one document."""
    seen: dict[Box, None] = {}
    for c in _all_citations(pkg):
        if c.source_file == source_file and c.page == page and c.bbox is not None:
            seen[c.bbox] = None
    return list(seen)


def source_format(pkg: DealPackage, source_file: str) -> str | None:
    if source_file == pkg.rent_roll.source_file:
        return pkg.rent_roll.source_format
    if pkg.t12 and source_file == pkg.t12.source_file:
        return pkg.t12.source_format
    return None


def rows_on_page(pkg: DealPackage, source_file: str, page: int | None) -> list[dict[str, str]]:
    """The extracted rows that came from this page (every row, for a workbook)."""
    rows: list[dict[str, str]] = []
    if source_file == pkg.rent_roll.source_file:
        for unit in pkg.rent_roll.units:
            if page is not None and unit.citations["unit_id"].page != page:
                continue
            rows.append(
                {
                    "Unit": unit.unit_id,
                    "Type": unit.unit_type or "",
                    "Status": unit.status,
                    "In-place": "" if unit.in_place_rent is None else f"{unit.in_place_rent:,.2f}",
                    "Market": "" if unit.market_rent is None else f"{unit.market_rent:,.2f}",
                    "Source": unit.citations["unit_id"].short(),
                }
            )
    elif pkg.t12 and source_file == pkg.t12.source_file:
        for line in pkg.t12.lines:
            if page is not None and line.citation.page != page:
                continue
            rows.append(
                {
                    "Line": line.label,
                    "Bucket": line.bucket,
                    "Mapped by": line.mapping_source,
                    "Total": f"{line.computed_total():,.2f}",
                    "Source": line.citation.short(),
                }
            )
    return rows


def audit_image(
    deal_dir: Path, pkg: DealPackage, focus: dict[str, Any]
) -> tuple[bytes | None, str]:
    """(page image with citation boxes, caption) for the focused citation.

    Text-layer pages show every citation box on the page with the focused one highlighted.
    Scanned pages show the scan with no boxes. Workbooks and manual entries have no page to draw.
    """
    source = focus["source_file"]
    fmt = source_format(pkg, source)
    path = deal_dir / source
    if focus.get("page") is None or fmt not in ("pdf_text", "pdf_scanned") or not path.exists():
        where = focus["short"] if focus else ""
        return None, f"{where}: this source has no page image (see the rows on the right)."
    png = render.render_page_png(path, focus["page"], dpi=RENDER_DPI)
    if fmt == "pdf_scanned":
        return png, "values transcribed by vision model"
    scale = RENDER_DPI / 72
    highlight = [tuple(focus["bbox"])] if focus.get("bbox") else []
    boxed = render.draw_boxes(
        png,
        boxes_on_page(pkg, source, focus["page"]),
        scale,
        highlight=highlight,  # type: ignore[arg-type]
    )
    return boxed, f"{source} page {focus['page']}"


# ---------- reviewing ----------


def validate_note(note: str, thresholds: ThresholdsConfig) -> str | None:
    """An error message when the note is too short, else None."""
    minimum = thresholds.review.override_note_min_chars
    if len(note.strip()) < minimum:
        return f"A reviewer note needs at least {minimum} characters."
    return None


def finding_override(
    finding: Finding, action: Literal["clear", "override"], note: str, now: datetime | None = None
) -> Override:
    """An override aimed at one finding: its first unit or line, else every finding of its check."""
    target = (finding.unit_ids or finding.line_labels or ["*"])[0]
    return Override(
        check_id=finding.check_id,
        action=action,
        target=target,
        note=note.strip(),
        at=now or datetime.now(UTC),
    )


# ---------- resolving a footing failure ----------

STRATEGY_LABELS = {
    "use_calculated_sum": "Use the calculated sum: the printed total is wrong",
    "use_stated_total": "Use the stated total: correct one unit so the rows sum to it",
    "edit_line_item": "Edit one line item: fix a unit that was misread, whatever the total",
}


@dataclass(frozen=True)
class FootingFacts:
    """The two numbers a footing reviewer has to choose between."""

    column: Literal["in_place_rent", "market_rent"]
    stated: Decimal
    calculated: Decimal
    grand_total: bool  # only a grand total can be replaced by the calculated sum

    @property
    def difference(self) -> Decimal:
        return self.calculated - self.stated

    def strategies(self) -> list[str]:
        both = ["use_calculated_sum", "use_stated_total", "edit_line_item"]
        return both if self.grand_total else both[1:]


def footing_facts(finding: Finding) -> FootingFacts | None:
    """The stated and calculated figures of an RR_FOOTING finding, or None for other findings."""
    if finding.check_id != "RR_FOOTING" or "stated" not in finding.details:
        return None
    column = finding.details.get("column")
    if column not in ("in_place_rent", "market_rent"):
        return None
    grand_total = "basis" in finding.details
    calculated = finding.details["computed_all_units" if grand_total else "computed"]
    return FootingFacts(
        column,  # type: ignore[arg-type]
        Decimal(finding.details["stated"]),
        Decimal(calculated),
        grand_total,
    )


def parse_amount(raw: str) -> Decimal:
    """A typed dollar amount, exact to the cent. Floats never enter."""
    try:
        value = parse_money(raw)
    except (ParseError, ArithmeticError) as exc:
        raise ValueError(f"{raw!r} is not an amount of money.") from exc
    if value is None:
        raise ValueError("Enter the corrected amount.")
    if money_was_rounded(raw):
        raise ValueError("Amounts have at most two decimal places.")
    return value


def unit_value(pkg: DealPackage, unit_id: str, column: str) -> Decimal:
    unit = next(u for u in pkg.rent_roll.units if u.unit_id == unit_id)
    return getattr(unit, column) or Decimal("0.00")


def remaining_after_edit(
    facts: FootingFacts, pkg: DealPackage, unit_id: str, amount: Decimal
) -> Decimal:
    """Calculated minus stated once ``unit_id`` carries ``amount``. Zero means it foots."""
    return facts.difference - unit_value(pkg, unit_id, facts.column) + amount


def footing_overrides(
    pkg: DealPackage,
    finding: Finding,
    strategy: str,
    note: str,
    *,
    unit_id: str | None = None,
    amount: str = "",
    now: datetime | None = None,
) -> Override:
    """The override for one footing resolution. Raises ValueError with a reviewer-facing message."""
    facts = footing_facts(finding)
    if facts is None:
        raise ValueError("This finding is not a rent-roll footing failure.")
    if strategy not in facts.strategies():
        raise ValueError("Choose which number governs the model.")
    stamp = now or datetime.now(UTC)
    base = {"check_id": "RR_FOOTING", "note": note.strip(), "at": stamp}
    if strategy == "use_calculated_sum":
        return Override(
            action="resolve_footing",
            target=facts.column,
            corrected_value=to_cents(facts.calculated),
            resolution_strategy="use_calculated_sum",
            **base,  # type: ignore[arg-type]
        )
    if not unit_id:
        raise ValueError("Choose the unit to correct.")
    value = parse_amount(amount)
    if strategy == "use_stated_total":
        left = remaining_after_edit(facts, pkg, unit_id, value)
        if left != 0:
            raise ValueError(
                f"With that amount the rows still miss the stated total by {left}. "
                "Enter the amount that makes them sum to it."
            )
    return Override(
        action="edit_value",
        target=edit_target(unit_id, facts.column),
        corrected_value=value,
        resolution_strategy=strategy,  # type: ignore[arg-type]
        **base,  # type: ignore[arg-type]
    )


def remap_changes(pkg: DealPackage, edited: dict[str, str]) -> list[tuple[str, str, str]]:
    """(label, current bucket, new bucket) for every line the reviewer moved."""
    if pkg.t12 is None:
        return []
    return [
        (line.label, line.bucket, edited[line.label])
        for line in pkg.t12.lines
        if line.label in edited and edited[line.label] != line.bucket
    ]


def remap_overrides(
    changes: Sequence[tuple[str, str, str]], note: str, now: datetime | None = None
) -> list[Override]:
    stamp = now or datetime.now(UTC)
    for _label, _old, new in changes:
        if new not in BUCKETS:
            raise ValueError(f"unknown bucket {new!r}")
    return [
        Override(
            check_id="T12_UNMAPPED",
            action="remap_line",
            target=remap_target(label, new),
            note=note.strip(),
            at=stamp,
        )
        for label, _old, new in changes
    ]


def t12_rows(pkg: DealPackage) -> list[dict[str, Any]]:
    if pkg.t12 is None:
        return []
    return [
        {
            "Label": ln.label,
            "Bucket": ln.bucket,
            "Mapped by": ln.mapping_source,
            "Confidence": round(ln.mapping_confidence, 2),
            "Annual total": f"{ln.computed_total():,.2f}",
        }
        for ln in pkg.t12.lines
    ]


def summary_metrics(pkg: DealPackage) -> dict[str, Decimal]:
    """Occupancy, loss to lease and the NOI walk, straight from the workbook's Python mirror."""
    uw = compute_uw(pkg)
    return {
        "physical_occupancy": uw["E35"],
        "economic_occupancy": uw["E36"],
        "monthly_loss_to_lease": uw["E37"],
        "noi_t12": uw["B24"],
        "noi_t3": uw["C24"],
        "noi_broker": uw["D24"],
        "noi_underwritten": uw["E24"],
        "dscr_underwritten": uw["E32"],
        "purchase_price": uw["E28"],
    }
