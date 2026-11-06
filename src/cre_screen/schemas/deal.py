from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import COABucket, Money, Status
from .findings import ExpectedFinding, Finding, GateState, GateTransition, Override
from .proforma import BrokerProForma
from .rent_roll import RentRoll
from .t12 import T12Statement


class DealPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    run_id: str  # "<UTC yyyymmddTHHMMSS>-<8 hex>"
    model_id: str
    rent_roll: RentRoll
    t12: T12Statement | None
    proforma: BrokerProForma | None
    findings: list[Finding]
    gate_log: list[GateTransition]
    final_state: GateState
    overrides: list[Override] = []
    cost_usd: Decimal = Decimal("0")
    wall_seconds: float = 0.0
    created_at: datetime

    def open_blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block" and f.status == "open"]


# ---------- synthetic ground truth ----------


class TruthUnit(BaseModel):
    """Values exactly as the DOCUMENT shows them (traps included)."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_type: str | None
    beds: int | None
    baths: Decimal | None
    sq_ft: int | None
    status: Status
    in_place_rent: Money | None
    market_rent: Money | None
    lease_start: date | None
    lease_end: date | None
    concession_note: str | None = None
    page: int | None = None  # PDF page where the row is printed


class TruthT12Line(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    true_bucket: COABucket
    monthly: list[Money] = Field(min_length=12, max_length=12)
    stated_total: Money
    is_nonrecurring: bool


class DealTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    seed: int
    split: Literal["dev", "heldout"]
    rr_layout: str
    t12_layout: str
    rr_format: Literal["pdf_text", "pdf_scanned", "xlsx"]
    t12_format: Literal["pdf_text", "xlsx"]
    as_of_date: date
    units: list[TruthUnit]
    rr_stated_totals: dict[str, Money]
    t12_lines: list[TruthT12Line]
    traps: list[str]
    expected: list[ExpectedFinding]


class DealScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    split: str
    provenance_mix: dict[str, int]  # provenance -> numeric fields scored
    numeric_fields_total: int
    numeric_fields_correct: int
    units_in_doc: int
    units_found: int
    phantom_units: int
    coa_dollars_total: Decimal
    coa_dollars_correct: Decimal
    planted: int
    caught: int
    false_positives: int  # warn/block findings matching no trap
    clean_control_findings: int  # warn/block findings on a clean deal
    cost_usd: Decimal
    wall_seconds: float
    misses: list[str]  # "trap_id: reason"
