"""Small constructors for check tests: units, rent rolls, T-12 lines, pro formas, contexts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from cre_screen.config import ThresholdsConfig, load_config
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.proforma import BrokerProForma
from cre_screen.schemas.rent_roll import (
    ColumnMap,
    Concession,
    HeaderMap,
    RentRoll,
    RentRollUnit,
    SubtotalRow,
)
from cre_screen.schemas.t12 import StatedSubtotal, T12Line, T12Statement

D = Decimal
THRESHOLDS = load_config().thresholds
AS_OF = date(2026, 9, 30)
MONTHS = [f"M{i}" for i in range(1, 13)]


def cite(row: int = 0, page: int = 1, raw: str = "x") -> Citation:
    return Citation(
        source_file="doc.pdf", page=page, row_index=row, raw_text=raw, provenance="text_layer"
    )


def money(value: str | None) -> Decimal | None:
    return None if value is None else D(value)


def unit(
    unit_id: str,
    rent: str | None = "1000.00",
    *,
    status: str = "occupied",
    market: str | None = None,
    unit_type: str | None = "1x1",
    beds: int | None = None,
    baths: str | None = None,
    lease_end: date | None = date(2027, 6, 30),
    concessions: list[Concession] | None = None,
    flags: list[str] | None = None,
    row: int = 0,
) -> RentRollUnit:
    return RentRollUnit(
        unit_id=unit_id,
        unit_type=unit_type,
        beds=beds,
        baths=money(baths),
        status=status,  # type: ignore[arg-type]
        in_place_rent=money(rent),
        market_rent=money(market),
        lease_end=lease_end,
        concessions=concessions or [],
        audit_flags=flags or [],
        citations={"unit_id": cite(row), "in_place_rent": cite(row, raw=str(rent))},
    )


def concession(
    monthly: str, *, grounded: bool = True, kind: str = "monthly_discount"
) -> Concession:
    return Concession(
        kind=kind,  # type: ignore[arg-type]
        monthly_equivalent=D(monthly),
        monthly_discount=D(monthly) if kind == "monthly_discount" else None,
        raw_text="note",
        grounded=grounded,
        citation=cite(),
    )


def roll(
    units: list[RentRollUnit],
    *,
    in_place_total: str | None = None,
    subtotals: list[SubtotalRow] | None = None,
) -> RentRoll:
    totals: dict[Any, Decimal] = {}
    cites: dict[Any, Citation] = {}
    if in_place_total is not None:
        totals["in_place_rent"] = D(in_place_total)
        cites["in_place_rent"] = cite(99)
    return RentRoll(
        source_file="doc.pdf",
        source_format="pdf_text",
        header_map=HeaderMap(
            columns=[
                ColumnMap(header_text="Unit", maps_to="unit_id"),
                ColumnMap(header_text="Status", maps_to="status"),
                ColumnMap(header_text="Rent", maps_to="in_place_rent"),
            ],
            source="synonym_table",
        ),
        units=units,
        stated_totals=totals,
        stated_total_citations=cites,
        subtotals=subtotals or [],
    )


def subtotal(
    label: str, stated: str, covers: list[str], scope: str = "page", column: str = "in_place_rent"
) -> SubtotalRow:
    return SubtotalRow(
        label=label,
        column=column,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        stated_value=D(stated),
        covers_unit_ids=covers,
        citation=cite(50),
    )


def line(
    label: str,
    bucket: str,
    monthly: str | list[str] = "100.00",
    stated: str | None = None,
    *,
    nonrecurring: bool = False,
    row: int = 0,
) -> T12Line:
    months = [monthly] * 12 if isinstance(monthly, str) else monthly
    return T12Line(
        label=label,
        bucket=bucket,  # type: ignore[arg-type]
        monthly=[D(m) for m in months],
        stated_total=money(stated),
        is_nonrecurring=nonrecurring,
        mapping_source="synonym_table",
        mapping_confidence=1.0,
        citation=cite(row),
    )


def stated_subtotal(
    label: str, kind: str, monthly: str | list[str | None], total: str | None
) -> StatedSubtotal:
    months = [monthly] * 12 if isinstance(monthly, str) else monthly
    return StatedSubtotal(
        label=label,
        kind=kind,  # type: ignore[arg-type]
        monthly=[money(m) for m in months],
        total=money(total),
        citation=cite(60),
    )


def t12(lines: list[T12Line], subtotals: list[StatedSubtotal] | None = None) -> T12Statement:
    return T12Statement(
        period_end=date(2026, 12, 31),
        month_labels=MONTHS,
        source_file="t12.pdf",
        source_format="pdf_text",
        lines=lines,
        stated_subtotals=subtotals or [],
    )


def proforma(
    lines: dict[str, str],
    *,
    vacancy_rate: str | None = None,
    mgmt_fee_pct: str | None = None,
) -> BrokerProForma:
    return BrokerProForma(
        property_name="Test Court",
        lines={k: D(v) for k, v in lines.items()},  # type: ignore[misc]
        vacancy_rate=money(vacancy_rate),
        mgmt_fee_pct=money(mgmt_fee_pct),
        source_note="test",
        citation=Citation(
            source_file="proforma.yaml", row_index=0, raw_text="test", provenance="manual_entry"
        ),
    )


def tweak(section: str, **changes: Any) -> ThresholdsConfig:
    """Thresholds with one section's values replaced."""
    current = getattr(THRESHOLDS, section)
    return THRESHOLDS.model_copy(update={section: current.model_copy(update=changes)})


def context(
    rr: RentRoll,
    *,
    t12: T12Statement | None = None,
    pf: BrokerProForma | None = None,
    thresholds: ThresholdsConfig | None = None,
    as_of: date = AS_OF,
) -> CheckContext:
    return CheckContext(
        rent_roll=rr,
        t12=t12,
        proforma=pf,
        thresholds=thresholds or THRESHOLDS,
        as_of=as_of,
    )
