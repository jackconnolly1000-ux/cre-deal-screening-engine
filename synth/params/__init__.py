"""Typed loader for ``synth/params/base.yaml``."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

BASE_YAML = Path(__file__).resolve().parent / "base.yaml"


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntRange(_P):
    min: int
    max: int


class DecRange(_P):
    min: Decimal
    max: Decimal


class ModelOrEmployee(_P):
    max: int


class Params(_P):
    unit_count: IntRange
    unit_mix: dict[str, Decimal]
    sq_ft: dict[str, tuple[int, int]]
    market_rent_per_sf: DecRange
    inplace_vs_market: DecRange
    rent_noise_within_type: Decimal
    physical_vacancy: DecRange
    model_or_employee_units: ModelOrEmployee
    other_income_per_unit_month: DecRange
    opex_per_unit_year: dict[str, tuple[Decimal, Decimal]]
    mgmt_fee_pct_of_egi: DecRange
    capex_per_unit_year: tuple[Decimal, Decimal]

    @field_validator("unit_mix", mode="before")
    @classmethod
    def _shares_via_str(cls, value: Any) -> Any:
        # YAML parses 0.10 as a float; go through str so the Decimal is exactly 0.10.
        return {k: Decimal(str(v)) for k, v in value.items()}

    @model_validator(mode="after")
    def _mix_sums_to_one(self) -> Params:
        if sum(self.unit_mix.values(), Decimal("0")) != Decimal("1"):
            raise ValueError("unit_mix shares must sum to 1")
        if set(self.unit_mix) != set(self.sq_ft):
            raise ValueError("unit_mix and sq_ft must have the same unit types")
        return self


def load_params(path: Path = BASE_YAML) -> Params:
    return Params.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
