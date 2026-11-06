"""CheckContext: the only input a check sees. Checks are pure functions of it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from cre_screen.config import ThresholdsConfig
from cre_screen.schemas.findings import Finding
from cre_screen.schemas.proforma import BrokerProForma
from cre_screen.schemas.rent_roll import RentRoll
from cre_screen.schemas.t12 import T12Statement


@dataclass(frozen=True)
class CheckContext:
    rent_roll: RentRoll
    t12: T12Statement | None
    proforma: BrokerProForma | None
    thresholds: ThresholdsConfig
    as_of: date


Check = Callable[[CheckContext], list[Finding]]
