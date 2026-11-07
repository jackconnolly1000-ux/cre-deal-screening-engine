"""PF_MGMT_FEE: pro forma management fee percentage below what the T-12 actually paid."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import pf_revenue_total, t12_egi_uw
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    pf, t12 = ctx.proforma, ctx.t12
    if pf is None or t12 is None:
        return []
    cfg = ctx.thresholds.pf_mgmt_fee
    if pf.mgmt_fee_pct is not None:
        projected = pf.mgmt_fee_pct
    else:
        fee = pf.lines.get("MANAGEMENT_FEE")  # type: ignore[call-overload]
        revenue = pf_revenue_total(pf)
        if fee is None or revenue <= 0:
            return []
        projected = fee / revenue
    egi = t12_egi_uw(t12)
    if egi <= 0:
        return []
    actual = t12.bucket_total("MANAGEMENT_FEE", uw_only=True) / egi
    if projected >= actual - cfg.margin:
        return []
    return [
        Finding(
            check_id="PF_MGMT_FEE",
            severity="warn",
            message=(
                f"the pro forma management fee is {(projected * 100).quantize(Decimal('0.1'))}% of "
                f"revenue; the T-12 paid {(actual * 100).quantize(Decimal('0.1'))}%"
            ),
            citations=[pf.citation]
            + [ln.citation for ln in t12.lines if ln.bucket == "MANAGEMENT_FEE"],
            details={
                "pro_forma": str(projected.quantize(Decimal("0.0001"))),
                "t12": str(actual.quantize(Decimal("0.0001"))),
            },
        )
    ]
