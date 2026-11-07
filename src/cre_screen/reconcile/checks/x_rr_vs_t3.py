"""X_RR_VS_T3: rent roll annualized vs the T-12 last three months, rental buckets only."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import ZERO, rr_inplace_monthly
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    if ctx.t12 is None:
        return []
    cfg = ctx.thresholds.x_rr_vs_t3
    rr_annual = 12 * rr_inplace_monthly(ctx.rent_roll.units, cfg.occupied_like_statuses)
    t3_annual = sum((ctx.t12.bucket_t3_annualized(b) for b in cfg.t3_rental_buckets), ZERO)
    if t3_annual <= 0:
        return []
    gap = abs(rr_annual - t3_annual) / t3_annual
    if gap <= cfg.gap_warn_pct:
        return []
    direction = "above" if rr_annual > t3_annual else "below"
    percent = (gap * 100).quantize(Decimal("0.1"))
    return [
        Finding(
            check_id="X_RR_VS_T3",
            severity="warn",
            message=(
                f"the rent roll annualizes to {to_cents(rr_annual)}, {percent}% {direction} "
                f"the T-12 last three months ({to_cents(t3_annual)})"
            ),
            delta=to_cents(rr_annual - t3_annual),
            citations=[ln.citation for ln in ctx.t12.lines if ln.bucket in cfg.t3_rental_buckets],
            details={
                "rr_annual": str(to_cents(rr_annual)),
                "t3_annual": str(to_cents(t3_annual)),
                "gap": str(gap.quantize(Decimal("0.0001"))),
            },
        )
    ]
