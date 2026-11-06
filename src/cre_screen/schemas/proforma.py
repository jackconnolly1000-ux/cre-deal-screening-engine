from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .citation import Citation
from .common import COABucket, Money, Rate


class BrokerProForma(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_name: str
    asking_price: Money | None = Field(default=None, gt=Decimal("0"))  # None if unpriced
    unit_count: int | None = Field(default=None, ge=1)
    lines: dict[COABucket, Money]  # annual, coa sign conventions
    vacancy_rate: Rate | None = None  # if the broker states a %
    mgmt_fee_pct: Rate | None = None  # if the broker states a %
    source_note: str  # e.g. "OM p.14 pro forma table"
    citation: Citation  # provenance="manual_entry"

    @model_validator(mode="after")
    def _no_control_buckets(self) -> BrokerProForma:
        bad = {"NON_OPERATING", "UNMAPPED"} & set(self.lines)
        if bad:
            raise ValueError(f"pro forma cannot use control buckets: {sorted(bad)}")
        return self
