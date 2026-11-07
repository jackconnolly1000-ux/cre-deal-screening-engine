"""RR_DUP_UNIT: a unit id printed more than once."""

from __future__ import annotations

from collections import defaultdict

from cre_screen.reconcile.context import CheckContext
from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.schemas.findings import Finding


def check(ctx: CheckContext) -> list[Finding]:
    groups: dict[str, list[int]] = defaultdict(list)
    units = ctx.rent_roll.units
    for index, unit in enumerate(units):
        groups[normalize_unit_id(unit.unit_id)].append(index)
    findings: list[Finding] = []
    for key, indexes in groups.items():
        if len(indexes) < 2:
            continue
        copies = [units[i] for i in indexes]
        findings.append(
            Finding(
                check_id="RR_DUP_UNIT",
                severity="block",
                message=(
                    f"unit {copies[0].unit_id} is printed {len(copies)} times "
                    f"({', '.join(c.citations['unit_id'].short() for c in copies)})"
                ),
                unit_ids=[copies[0].unit_id],
                citations=[c.citations["unit_id"] for c in copies],
                details={"normalized_id": key, "copies": str(len(copies))},
            )
        )
    return findings
