"""PF_VACANCY: pro forma vacancy below the rent roll physical vacancy."""

from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import physical_vacancy
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    pf = ctx.proforma
    if pf is None:
        return []
    cfg = ctx.thresholds.pf_vacancy
    if pf.vacancy_rate is not None:
        projected = pf.vacancy_rate
    else:
        loss = pf.lines.get("VACANCY_LOSS")  # type: ignore[call-overload]
        gpr = pf.lines.get("GROSS_POTENTIAL_RENT")  # type: ignore[call-overload]
        if loss is None or gpr is None or gpr == 0:
            return []
        projected = -loss / gpr
    actual = physical_vacancy(ctx.rent_roll.units, cfg.vacant_like_statuses)
    if actual is None or projected >= actual - cfg.margin:
        return []
    return [
        Finding(
            check_id="PF_VACANCY",
            severity="warn",
            message=(
                f"the pro forma assumes {(projected * 100).quantize(Decimal('0.1'))}% vacancy "
                f"but the rent roll shows "
                f"{(actual * 100).quantize(Decimal('0.1'))}% physical vacancy"
            ),
            citations=[pf.citation],
            details={
                "pro_forma": str(projected.quantize(Decimal("0.0001"))),
                "physical": str(actual.quantize(Decimal("0.0001"))),
            },
        )
    ]
