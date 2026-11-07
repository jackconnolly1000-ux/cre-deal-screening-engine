"""RR_ZERO_RENT_OCC: occupied units paying no rent."""

from __future__ import annotations

from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.findings import Finding
from cre_screen.schemas.rent_roll import RentRollUnit

NON_RESIDENT_STATUSES = ("model", "employee")


def _zero(unit: RentRollUnit) -> bool:
    return unit.in_place_rent is None or unit.in_place_rent == 0


def check(ctx: CheckContext) -> list[Finding]:
    occupied_like = set(ctx.thresholds.rr_zero_rent_occ.occupied_like_statuses)
    units = ctx.rent_roll.units
    flagged = [u for u in units if u.status in occupied_like and _zero(u)]
    non_resident = [u for u in units if u.status in NON_RESIDENT_STATUSES and _zero(u)]
    findings: list[Finding] = []
    if flagged:
        findings.append(
            Finding(
                check_id="RR_ZERO_RENT_OCC",
                severity="warn",
                message=(
                    f"{len(flagged)} occupied unit(s) show no in-place rent: "
                    + ", ".join(u.unit_id for u in flagged)
                ),
                unit_ids=[u.unit_id for u in flagged],
                citations=[u.citations["unit_id"] for u in flagged],
                details={"count": str(len(flagged))},
            )
        )
    if non_resident:
        findings.append(
            Finding(
                check_id="RR_ZERO_RENT_OCC",
                severity="info",
                message=(
                    f"{len(non_resident)} model/employee unit(s) carry $0 rent, as expected: "
                    + ", ".join(u.unit_id for u in non_resident)
                ),
                unit_ids=[u.unit_id for u in non_resident],
                citations=[u.citations["unit_id"] for u in non_resident],
                details={"count": str(len(non_resident))},
            )
        )
    return findings
