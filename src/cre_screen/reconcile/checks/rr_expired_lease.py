"""RR_EXPIRED_LEASE: occupied units whose lease ended before the as-of date."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    cfg = ctx.thresholds.rr_expired_lease
    statuses = set(cfg.occupied_like_statuses)
    occupied_like = [u for u in ctx.rent_roll.units if u.status in statuses]
    flagged = []
    for unit in occupied_like:
        if unit.lease_end is None:
            if cfg.treat_missing_lease_end_as_mtm:
                flagged.append(unit)
        elif unit.lease_end < ctx.as_of:
            flagged.append(unit)
    if not flagged:
        return []
    share = Decimal(len(flagged)) / Decimal(len(occupied_like))
    return [
        Finding(
            check_id="RR_EXPIRED_LEASE",
            severity="warn",
            message=(
                f"{len(flagged)} occupied unit(s) have no current lease on "
                f"{ctx.as_of.isoformat()} (expired or month-to-month): "
                + ", ".join(u.unit_id for u in flagged)
            ),
            unit_ids=[u.unit_id for u in flagged],
            citations=[u.citations["unit_id"] for u in flagged],
            details={
                "mtm_share": str(share.quantize(Decimal("0.0001"))),
                "count": str(len(flagged)),
                "occupied_like_count": str(len(occupied_like)),
            },
        )
    ]
