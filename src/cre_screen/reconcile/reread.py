"""Footing localization and re-read comparison: the pure half of the REEXTRACT step.

The re-reading itself touches files (and, for scanned pages, the LLM), so it lives in
``ingest/reread.py`` and is injected into the gate as a ``Rereader``. ``reconcile/`` stays free of
I/O and LLM calls.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from cre_screen.reconcile.metrics import ZERO, units_covered
from cre_screen.schemas.rent_roll import RentRoll, RentRollUnit

MONEY_FIELDS = ("in_place_rent", "market_rent")


@dataclass(frozen=True)
class RereadResult:
    agrees: bool
    disagreeing_unit_ids: list[str] = field(default_factory=list)


# Takes the block's units (as first extracted), returns whether an independent re-read agrees.
Rereader = Callable[[list[RentRollUnit]], RereadResult]


def localize(rent_roll: RentRoll, tolerance: Decimal) -> list[RentRollUnit]:
    """Units of the smallest failing in-place subtotal; the whole table if none is localizable."""
    failing: list[list[RentRollUnit]] = []
    for sub in rent_roll.subtotals:
        if sub.column != "in_place_rent" or sub.scope == "grand_total":
            continue
        block = units_covered(rent_roll.units, sub.covers_unit_ids)
        computed = sum((u.in_place_rent or ZERO for u in block), ZERO)
        if abs(computed - sub.stated_value) > tolerance:
            failing.append(block)
    if failing:
        return min(failing, key=len)
    return list(rent_roll.units)


def compare_money(
    units: Sequence[RentRollUnit], reread: Sequence[dict[str, Decimal | None]]
) -> RereadResult:
    """Agreement means every money field of every unit is equal to the cent."""
    if len(units) != len(reread):
        raise ValueError("re-read must return one entry per unit")
    bad: list[str] = []
    for unit, values in zip(units, reread, strict=True):
        for name in MONEY_FIELDS:
            if name in values and values[name] != getattr(unit, name):
                bad.append(unit.unit_id)
                break
    return RereadResult(agrees=not bad, disagreeing_unit_ids=list(dict.fromkeys(bad)))
