"""RR_FOOTING: stated rent roll totals and subtotals must equal the sum of the unit rows."""

from __future__ import annotations

from collections import defaultdict, deque
from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.common import Severity, to_cents
from cre_screen.schemas.findings import Finding
from cre_screen.schemas.rent_roll import RentRollUnit

COLUMNS = ("in_place_rent", "market_rent")
ZERO = Decimal("0")


def _rent(unit: RentRollUnit, column: str) -> Decimal:
    value = unit.in_place_rent if column == "in_place_rent" else unit.market_rent
    return ZERO if value is None else value


def _sum_covered(units: list[RentRollUnit], covers: list[str], column: str) -> Decimal:
    """Sum rows named by ``covers``; a unit id listed twice consumes two printed rows."""
    by_id: dict[str, deque[RentRollUnit]] = defaultdict(deque)
    for unit in units:
        by_id[unit.unit_id].append(unit)
    total = ZERO
    for unit_id in covers:
        queue = by_id.get(unit_id)
        if queue:
            total += _rent(queue.popleft(), column)
    return total


def check(ctx: CheckContext) -> list[Finding]:
    cfg = ctx.thresholds.rr_footing
    tolerance = cfg.tolerance_usd
    occupied_like = set(ctx.thresholds.rr_zero_rent_occ.occupied_like_statuses)
    units = ctx.rent_roll.units
    findings: list[Finding] = []

    def severity_for(column: str) -> Severity:
        return "block" if column == "in_place_rent" else cfg.market_rent_severity

    verifiable = False
    for column in COLUMNS:
        stated = ctx.rent_roll.stated_totals.get(column)  # type: ignore[call-overload]
        if stated is None:
            continue
        verifiable = verifiable or column == "in_place_rent"
        all_units = sum((_rent(u, column) for u in units), ZERO)
        occupied = sum((_rent(u, column) for u in units if u.status in occupied_like), ZERO)
        deltas = {"all_units": all_units - stated, "occupied_like": occupied - stated}
        basis = min(deltas, key=lambda b: abs(deltas[b]))
        if abs(deltas[basis]) <= tolerance:
            continue
        citation = ctx.rent_roll.stated_total_citations[column]  # type: ignore[index]
        findings.append(
            Finding(
                check_id="RR_FOOTING",
                severity=severity_for(column),
                message=(
                    f"{column} does not foot: stated total {stated} but the unit rows sum to "
                    f"{all_units} ({basis.replace('_', ' ')} basis off by {deltas[basis]})"
                ),
                delta=to_cents(deltas[basis]),
                citations=[citation],
                details={
                    "column": column,
                    "basis": basis,
                    "stated": str(stated),
                    "computed_all_units": str(to_cents(all_units)),
                    "computed_occupied_like": str(to_cents(occupied)),
                },
            )
        )

    for sub in ctx.rent_roll.subtotals:
        if sub.scope == "grand_total":
            continue  # covered by stated_totals above
        verifiable = verifiable or sub.column == "in_place_rent"
        computed = _sum_covered(units, sub.covers_unit_ids, sub.column)
        delta = computed - sub.stated_value
        if abs(delta) <= tolerance:
            continue
        findings.append(
            Finding(
                check_id="RR_FOOTING",
                severity=severity_for(sub.column),
                message=(
                    f"{sub.scope} subtotal '{sub.label}' ({sub.column}) does not foot: stated "
                    f"{sub.stated_value} but its rows sum to {computed}"
                ),
                delta=to_cents(delta),
                unit_ids=list(dict.fromkeys(sub.covers_unit_ids)),
                citations=[sub.citation],
                details={
                    "column": sub.column,
                    "scope": sub.scope,
                    "stated": str(sub.stated_value),
                    "computed": str(to_cents(computed)),
                },
            )
        )

    if not verifiable and not findings:
        findings.append(
            Finding(
                check_id="RR_FOOTING",
                severity="info",
                message="footing not verifiable: the rent roll states no in-place rent total",
            )
        )
    return findings
