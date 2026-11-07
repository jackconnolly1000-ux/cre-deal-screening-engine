"""Pure helper functions shared by checks and the gate. No I/O."""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from decimal import Decimal

from cre_screen.schemas.common import OPEX_BUCKETS, REVENUE_BUCKETS
from cre_screen.schemas.proforma import BrokerProForma
from cre_screen.schemas.rent_roll import RentRollUnit
from cre_screen.schemas.t12 import T12Statement

ZERO = Decimal("0")


def normalize_unit_id(unit_id: str) -> str:
    """Strip, uppercase, collapse internal whitespace. Leading zeros are kept."""
    return " ".join(unit_id.strip().upper().split())


def unique_units(units: Sequence[RentRollUnit]) -> list[RentRollUnit]:
    """First printed row per normalized unit id.

    A duplicated row is a document artefact (RR_DUP_UNIT reports it); counting it twice would
    skew occupancy and the rent roll vs T-3 comparison.
    """
    seen: set[str] = set()
    out: list[RentRollUnit] = []
    for unit in units:
        key = normalize_unit_id(unit.unit_id)
        if key not in seen:
            seen.add(key)
            out.append(unit)
    return out


def units_covered(units: Sequence[RentRollUnit], covers: Sequence[str]) -> list[RentRollUnit]:
    """Rows named by a subtotal's ``covers_unit_ids``; an id listed twice consumes two rows."""
    by_id: dict[str, deque[RentRollUnit]] = defaultdict(deque)
    for unit in units:
        by_id[unit.unit_id].append(unit)
    out: list[RentRollUnit] = []
    for unit_id in covers:
        queue = by_id.get(unit_id)
        if queue:
            out.append(queue.popleft())
    return out


def rr_inplace_monthly(units: Sequence[RentRollUnit], occupied_like: Iterable[str]) -> Decimal:
    """Sum of in-place rent (None counts as 0) over occupied-like units."""
    statuses = set(occupied_like)
    return sum((u.in_place_rent or ZERO for u in unique_units(units) if u.status in statuses), ZERO)


def physical_vacancy(units: Sequence[RentRollUnit], vacant_like: Iterable[str]) -> Decimal | None:
    """Share of units with a vacant-like status, or None when there are no units."""
    rows = unique_units(units)
    if not rows:
        return None
    statuses = set(vacant_like)
    return Decimal(sum(1 for u in rows if u.status in statuses)) / Decimal(len(rows))


def bucket_section(bucket: str) -> str:
    """Section of a bucket; mirrors ``section`` in config/coa.yaml (a test asserts they agree)."""
    if bucket in REVENUE_BUCKETS:
        return "revenue"
    if bucket in OPEX_BUCKETS:
        return "opex"
    return {"CAPEX_RESERVES": "below_noi", "NON_OPERATING": "excluded"}.get(bucket, "control")


def t12_egi_uw(t12: T12Statement) -> Decimal:
    """Underwritten effective gross income: revenue buckets, recurring and mapped lines only."""
    return sum((t12.bucket_total(b, uw_only=True) for b in REVENUE_BUCKETS), ZERO)


def pf_revenue_total(proforma: BrokerProForma) -> Decimal:
    return sum((v for k, v in proforma.lines.items() if k in REVENUE_BUCKETS), ZERO)


def median(values: Sequence[Decimal]) -> Decimal:
    return Decimal(statistics.median(values))
