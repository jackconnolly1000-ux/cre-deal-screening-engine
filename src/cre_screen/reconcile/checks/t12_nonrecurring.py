"""T12_NONRECURRING: one-time items inside the T-12, excluded from underwritten NOI."""

from __future__ import annotations

from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    if ctx.t12 is None:
        return []
    lines = [ln for ln in ctx.t12.lines if ln.is_nonrecurring]
    if not lines:
        return []
    amounts = "; ".join(f"{ln.label} {ln.computed_total()}" for ln in lines)
    return [
        Finding(
            check_id="T12_NONRECURRING",
            severity="warn",
            message=f"one-time item(s) in the T-12, excluded from underwritten NOI: {amounts}",
            line_labels=[ln.label for ln in lines],
            citations=[ln.citation for ln in lines],
            details={ln.label: str(ln.computed_total()) for ln in lines},
        )
    ]
