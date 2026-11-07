"""RR_RENT_OUTLIER: rents far from the median of their unit type."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import median
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.findings import Finding
from cre_screen.schemas.rent_roll import RentRollUnit


def group_key(unit: RentRollUnit) -> str | None:
    if unit.unit_type:
        return unit.unit_type
    if unit.beds is not None and unit.baths is not None:
        return f"{unit.beds}x{format(unit.baths.normalize(), 'f')}"
    return None


def check(ctx: CheckContext) -> list[Finding]:
    cfg = ctx.thresholds.rr_rent_outlier
    groups: dict[str, list[tuple[RentRollUnit, Decimal]]] = defaultdict(list)
    for unit in ctx.rent_roll.units:
        key = group_key(unit)
        if key is not None and unit.in_place_rent is not None and unit.in_place_rent > 0:
            groups[key].append((unit, unit.in_place_rent))
    findings: list[Finding] = []
    for key, members in groups.items():
        if len(members) < cfg.min_group_size:
            continue
        mid = median([rent for _unit, rent in members])
        outliers = [u for u, rent in members if abs(rent - mid) / mid > cfg.deviation_pct]
        if not outliers:
            continue
        percent = (cfg.deviation_pct * 100).normalize()
        findings.append(
            Finding(
                check_id="RR_RENT_OUTLIER",
                severity="warn",
                message=(
                    f"{len(outliers)} {key} unit(s) are more than {percent:f}% from the type "
                    f"median {to_cents(mid)}: " + ", ".join(u.unit_id for u in outliers)
                ),
                unit_ids=[u.unit_id for u in outliers],
                citations=[
                    u.citations.get("in_place_rent", u.citations["unit_id"]) for u in outliers
                ],
                details={
                    "group": key,
                    "median": str(to_cents(mid)),
                    "group_size": str(len(members)),
                },
            )
        )
    return findings
