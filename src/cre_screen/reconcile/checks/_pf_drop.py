"""Shared logic for the two pro forma "drop vs T-12" checks (taxes and insurance)."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.common import CheckId, to_cents
from cre_screen.schemas.findings import Finding


def drop_finding(
    ctx: CheckContext,
    *,
    check_id: CheckId,
    bucket: str,
    drop_pct: Decimal,
    what: str,
    why: str,
) -> list[Finding]:
    if ctx.t12 is None or ctx.proforma is None:
        return []
    projected = ctx.proforma.lines.get(bucket)  # type: ignore[call-overload]
    if projected is None:
        return []
    actual = ctx.t12.bucket_total(bucket)
    floor = actual * (Decimal(1) - drop_pct)
    if projected >= floor:
        return []
    actual_drop = (actual - projected) / actual
    return [
        Finding(
            check_id=check_id,
            severity="warn",
            message=(
                f"the pro forma projects {what} of {to_cents(projected)}, "
                f"{(actual_drop * 100).quantize(Decimal('0.1'))}% below the T-12 "
                f"({to_cents(actual)}); "
                f"{why}"
            ),
            delta=to_cents(projected - actual),
            line_labels=[ln.label for ln in ctx.t12.lines if ln.bucket == bucket],
            citations=[ctx.proforma.citation]
            + [ln.citation for ln in ctx.t12.lines if ln.bucket == bucket],
            details={
                "t12": str(to_cents(actual)),
                "pro_forma": str(to_cents(projected)),
                "drop": str(actual_drop.quantize(Decimal("0.0001"))),
            },
        )
    ]
