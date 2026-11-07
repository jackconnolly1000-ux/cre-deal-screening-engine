"""RR_CONCESSION: concessions hidden in the notes column, rents shown gross."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import ZERO, rr_inplace_monthly
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    units = [
        u for u in ctx.rent_roll.units if u.concessions or "concession_ungrounded" in u.audit_flags
    ]
    if not units:
        return []
    cfg = ctx.thresholds.rr_concession
    monthly = sum((c.monthly_equivalent for u in units for c in u.concessions), ZERO)
    rent = rr_inplace_monthly(
        ctx.rent_roll.units, ctx.thresholds.rr_zero_rent_occ.occupied_like_statuses
    )
    drag = monthly / rent if rent > 0 else ZERO
    ungrounded = [
        u.unit_id
        for u in units
        if "concession_ungrounded" in u.audit_flags or any(not c.grounded for c in u.concessions)
    ]
    warn = drag > cfg.drag_warn_pct or bool(ungrounded)
    percent = (drag * 100).quantize(Decimal("0.01"))
    note = f"; {len(ungrounded)} note(s) could not be parsed and need review" if ungrounded else ""
    return [
        Finding(
            check_id="RR_CONCESSION",
            severity="warn" if warn else "info",
            message=(
                f"{len(units)} unit(s) carry concessions worth {to_cents(monthly)} a month "
                f"({percent}% of in-place rent); rents are shown gross{note}"
            ),
            delta=to_cents(monthly),
            unit_ids=[u.unit_id for u in units],
            citations=[c.citation for u in units for c in u.concessions]
            or [u.citations["unit_id"] for u in units],
            details={
                "drag": str(drag.quantize(Decimal("0.0001"))),
                "ungrounded_units": ",".join(ungrounded),
            },
        )
    ]
