"""Small deterministic helpers. Money is generated from integer draws, never from floats."""

from __future__ import annotations

import calendar
import os
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from random import Random

ZERO = Decimal("0")
ONE = Decimal("1")


def rand_dec(rng: Random, lo: Decimal, hi: Decimal, places: int = 2) -> Decimal:
    """Uniform Decimal in [lo, hi] on a grid of ``places`` decimal places."""
    step = ONE.scaleb(-places)
    lo_i = int((lo / step).to_integral_value(ROUND_CEILING))
    hi_i = int((hi / step).to_integral_value(ROUND_FLOOR))
    return Decimal(rng.randint(lo_i, hi_i)) * step


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def write_atomic(path: Path, data: bytes) -> None:
    """Write bytes (so Windows never rewrites newlines), then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
