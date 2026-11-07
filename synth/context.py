"""Mutable working state shared by the deal builder and the traps."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from random import Random
from typing import TYPE_CHECKING

from cre_screen.schemas.common import COABucket
from cre_screen.schemas.deal import TruthUnit
from cre_screen.schemas.findings import ExpectedFinding

if TYPE_CHECKING:
    from faker import Faker

    from synth.params import Params


@dataclass
class WorkLine:
    label: str
    bucket: COABucket
    monthly: list[Decimal]
    is_nonrecurring: bool = False
    frozen_total: Decimal | None = None  # set by TRAP_T12_FOOTING: stated total stays original

    def stated_total(self) -> Decimal:
        if self.frozen_total is not None:
            return self.frozen_total
        return sum(self.monthly, Decimal("0"))


@dataclass
class Sample:
    n_units: int
    psf: Decimal
    vac_rate: Decimal
    mgmt_pct: Decimal
    growth: Decimal  # T-12 months 1-9 are scaled down by up to this much
    pf_growth: Decimal  # pro forma lines sit this far above the T-12


@dataclass
class Style:
    rr_headers: dict[str, str]
    t12_label_header: str
    money_style: str
    t12_neg: str
    zero_dash: bool
    date_style: str
    month_style: str
    status_style: str
    property_name: str
    aux_seed: int = 0  # seeds the printed-only extras (tenant names, deposits) per deal


@dataclass
class BuildCtx:
    rng: Random
    fake: Faker
    params: Params
    deal_number: int
    as_of: date
    sample: Sample
    units: list[TruthUnit] = field(default_factory=list)
    lines: list[WorkLine] = field(default_factory=list)
    expected: list[ExpectedFinding] = field(default_factory=list)
    used_units: set[str] = field(default_factory=set)
    used_lines: set[str] = field(default_factory=set)
    rr_totals: dict[str, Decimal] = field(default_factory=dict)
    pf: dict[str, object] = field(default_factory=dict)
    rr_layout: str = "rr_classic"
    variant: str = "dev"  # "heldout" switches on the harder trap variants
    footnotes: list[str] = field(default_factory=list)  # printed under the rent roll table

    def pick_units(self, k: int, pred: Callable[[TruthUnit], bool]) -> list[TruthUnit]:
        pool = [u for u in self.units if u.unit_id not in self.used_units and pred(u)]
        chosen = self.rng.sample(pool, min(k, len(pool)))
        self.used_units.update(u.unit_id for u in chosen)
        return chosen
