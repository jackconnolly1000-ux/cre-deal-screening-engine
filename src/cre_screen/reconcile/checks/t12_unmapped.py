"""T12_UNMAPPED: lines no one has classified yet. Always blocks."""

from __future__ import annotations

from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    if ctx.t12 is None:
        return []
    lines = [ln for ln in ctx.t12.lines if ln.bucket == "UNMAPPED"]
    if not lines:
        return []
    return [
        Finding(
            check_id="T12_UNMAPPED",
            severity="block",
            message=f"{len(lines)} T-12 line(s) are not mapped to an account: "
            + ", ".join(ln.label for ln in lines),
            line_labels=[ln.label for ln in lines],
            citations=[ln.citation for ln in lines],
            details={"count": str(len(lines))},
        )
    ]
