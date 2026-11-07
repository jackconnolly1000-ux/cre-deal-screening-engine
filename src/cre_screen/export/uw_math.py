"""Pure-Python Decimal mirror of ``ProForma_NOI_DSCR!B4:E37``.

This is the "Python side" of the Excel-agrees-with-Python test. It reads the same package the
writer reads and applies the same formulas, so any drift between the workbook and this module is a
bug in one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from cre_screen.export import layout as L
from cre_screen.schemas.common import RENTAL_BUCKETS
from cre_screen.schemas.deal import DealPackage

D = Decimal
ZERO = D("0")
OCCUPIED_LIKE = ("occupied", "notice")


# SPEC 9.2 defaults, as module constants so the dataclass defaults are plain names.
DEFAULT_ENTRY_CAP_RATE = D("0.0625")
DEFAULT_LTV = D("0.65")
DEFAULT_INTEREST_RATE = D("0.065")
DEFAULT_EXPENSE_GROWTH = D("0.03")
DEFAULT_MGMT_FEE_PCT = D("0.03")
DEFAULT_RESERVES_PER_UNIT = D("250")
DEFAULT_REASSESSED_TAX_BUMP = D("1.15")


@dataclass(frozen=True)
class Assumptions:
    """SPEC 9.2 defaults for every blue input except the asking price (it comes from the deal)."""

    entry_cap_rate: Decimal = DEFAULT_ENTRY_CAP_RATE
    ltv: Decimal = DEFAULT_LTV
    interest_rate: Decimal = DEFAULT_INTEREST_RATE
    amort_years: int = 30
    io_period_months: int = 0
    expense_growth: Decimal = DEFAULT_EXPENSE_GROWTH
    mgmt_fee_pct: Decimal = DEFAULT_MGMT_FEE_PCT
    reserves_per_unit: Decimal = DEFAULT_RESERVES_PER_UNIT
    assessment_ratio: Decimal | None = None
    tax_rate: Decimal | None = None
    reassessed_tax_bump: Decimal = DEFAULT_REASSESSED_TAX_BUMP


def pmt(rate: Decimal, periods: int, principal: Decimal) -> Decimal:
    """Positive monthly payment of a fully amortizing loan (Excel's -PMT)."""
    if rate == 0:
        return principal / D(periods)
    return principal * rate / (1 - (1 + rate) ** -periods)


def _div(numerator: Decimal, denominator: Decimal) -> Decimal:
    """IFERROR(n/d, 0)."""
    return ZERO if denominator == 0 else numerator / denominator


def debt_service(loan: Decimal, a: Assumptions) -> Decimal:
    io = min(a.io_period_months, 12)
    interest_only = D(io) * loan * a.interest_rate / 12
    amortizing = D(12 - io) * pmt(a.interest_rate / 12, a.amort_years * 12, loan)
    return interest_only + amortizing


def compute_uw(pkg: DealPackage, assumptions: Assumptions | None = None) -> dict[str, Decimal]:
    """Every formula cell of ``ProForma_NOI_DSCR!B4:E37`` keyed by address, e.g. ``"E24"``."""
    a = assumptions or Assumptions()
    lines = pkg.t12.lines if pkg.t12 else []
    pf_lines = dict(pkg.proforma.lines) if pkg.proforma else {}
    units = pkg.rent_roll.units

    def q(bucket: str, *, inc_only: bool) -> Decimal:
        return sum(
            (
                ln.computed_total()
                for ln in lines
                if ln.bucket == bucket and (ln.include_in_uw or not inc_only)
            ),
            ZERO,
        )

    def s(bucket: str, *, inc_only: bool) -> Decimal:
        return sum(
            (
                ln.t3_annualized()
                for ln in lines
                if ln.bucket == bucket and (ln.include_in_uw or not inc_only)
            ),
            ZERO,
        )

    def pfv(bucket: str) -> Decimal:
        return pf_lines.get(bucket, ZERO)  # type: ignore[call-overload]

    out: dict[str, Decimal] = {}
    unit_count = D(len(units))
    occupied = D(sum(1 for u in units if u.status in OCCUPIED_LIKE))

    # Columns B (T-12), C (T-3 annualized), D (broker): plain SUMIFS per bucket.
    for bucket, row in {**L.REVENUE_ROWS, **L.OPEX_ROWS}.items():
        out[f"B{row}"] = q(bucket, inc_only=False)
        out[f"C{row}"] = s(bucket, inc_only=False)
        out[f"D{row}"] = pfv(bucket)
    out[f"B{L.ROW_RESERVES}"] = q("CAPEX_RESERVES", inc_only=False)
    out[f"C{L.ROW_RESERVES}"] = s("CAPEX_RESERVES", inc_only=False)
    out[f"D{L.ROW_RESERVES}"] = pfv("CAPEX_RESERVES")

    def subtotal(col: str) -> None:
        out[f"{col}{L.ROW_EGI}"] = sum((out[f"{col}{r}"] for r in L.REVENUE_ROWS.values()), ZERO)
        out[f"{col}{L.ROW_OPEX_TOTAL}"] = sum(
            (out[f"{col}{r}"] for r in L.OPEX_ROWS.values()), ZERO
        )
        out[f"{col}{L.ROW_NOI}"] = out[f"{col}{L.ROW_EGI}"] - out[f"{col}{L.ROW_OPEX_TOTAL}"]

    for col in ("B", "C", "D"):
        subtotal(col)

    # Assumption chain: PurchasePrice <- BrokerNOI (D24); ReassessedTax <- PurchasePrice, T12Taxes.
    asking = pkg.proforma.asking_price if pkg.proforma else None
    broker_noi = out[f"D{L.ROW_NOI}"]
    price = asking if asking is not None and asking > 0 else _div(broker_noi, a.entry_cap_rate)
    loan = price * a.ltv
    t12_taxes = out[f"B{L.OPEX_ROWS['REAL_ESTATE_TAXES']}"]
    if a.assessment_ratio and a.tax_rate and a.assessment_ratio > 0 and a.tax_rate > 0:
        reassessed = price * a.assessment_ratio * a.tax_rate
    else:
        reassessed = t12_taxes * a.reassessed_tax_bump

    # Column E (underwritten).
    for bucket, row in L.REVENUE_ROWS.items():
        source = s if bucket in RENTAL_BUCKETS else q
        out[f"E{row}"] = source(bucket, inc_only=True)
    out[f"E{L.ROW_EGI}"] = sum((out[f"E{r}"] for r in L.REVENUE_ROWS.values()), ZERO)
    grow = 1 + a.expense_growth
    for bucket, row in L.OPEX_ROWS.items():
        base = q(bucket, inc_only=True)
        if bucket == "REAL_ESTATE_TAXES":
            out[f"E{row}"] = max(base, reassessed)
        elif bucket == "MANAGEMENT_FEE":
            out[f"E{row}"] = max(base * grow, out[f"E{L.ROW_EGI}"] * a.mgmt_fee_pct)
        else:
            out[f"E{row}"] = base * grow
    out[f"E{L.ROW_OPEX_TOTAL}"] = sum((out[f"E{r}"] for r in L.OPEX_ROWS.values()), ZERO)
    out[f"E{L.ROW_NOI}"] = out[f"E{L.ROW_EGI}"] - out[f"E{L.ROW_OPEX_TOTAL}"]
    out[f"E{L.ROW_RESERVES}"] = a.reserves_per_unit * unit_count

    service = debt_service(loan, a)
    physical = _div(occupied, unit_count)
    loss_to_lease = sum(
        (
            (u.market_rent or ZERO) - (u.in_place_rent or ZERO)
            for u in units
            if u.status in OCCUPIED_LIKE
        ),
        ZERO,
    )
    for col in L.PF_COLUMNS:
        out[f"{col}{L.ROW_NCF}"] = out[f"{col}{L.ROW_NOI}"] - out[f"{col}{L.ROW_RESERVES}"]
        out[f"{col}{L.ROW_PRICE}"] = price
        out[f"{col}{L.ROW_CAP_RATE}"] = _div(out[f"{col}{L.ROW_NOI}"], price)
        out[f"{col}{L.ROW_LOAN}"] = loan
        out[f"{col}{L.ROW_DEBT_SERVICE}"] = service
        out[f"{col}{L.ROW_DSCR}"] = _div(out[f"{col}{L.ROW_NCF}"], service)
        out[f"{col}{L.ROW_DEBT_YIELD}"] = _div(out[f"{col}{L.ROW_NOI}"], loan)
        out[f"{col}{L.ROW_PHYS_OCC}"] = physical
        rental = sum((out[f"{col}{L.REVENUE_ROWS[b]}"] for b in RENTAL_BUCKETS), ZERO)
        out[f"{col}{L.ROW_ECON_OCC}"] = _div(
            rental, out[f"{col}{L.REVENUE_ROWS['GROSS_POTENTIAL_RENT']}"]
        )
        out[f"{col}{L.ROW_LTL}"] = loss_to_lease
    return out


__all__ = ["Assumptions", "compute_uw", "debt_service", "pmt"]
