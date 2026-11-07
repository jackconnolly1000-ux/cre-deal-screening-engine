"""PF_INS_DROP: pro forma insurance below the T-12."""

from __future__ import annotations

from cre_screen.reconcile.checks._pf_drop import drop_finding
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    return drop_finding(
        ctx,
        check_id="PF_INS_DROP",
        bucket="INSURANCE",
        drop_pct=ctx.thresholds.pf_ins_drop.drop_pct,
        what="insurance",
        why="a quote should back any drop in premiums",
    )
