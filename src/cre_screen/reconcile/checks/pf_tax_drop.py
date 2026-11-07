"""PF_TAX_DROP: pro forma taxes below the T-12."""

from __future__ import annotations

from cre_screen.reconcile.checks._pf_drop import drop_finding
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    return drop_finding(
        ctx,
        check_id="PF_TAX_DROP",
        bucket="REAL_ESTATE_TAXES",
        drop_pct=ctx.thresholds.pf_tax_drop.drop_pct,
        what="real estate taxes",
        why="a sale may trigger reassessment, so the drop needs support",
    )
