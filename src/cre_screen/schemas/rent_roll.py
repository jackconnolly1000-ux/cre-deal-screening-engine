from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .citation import Citation
from .common import Money, RRField, SourceFormat, Status


class Concession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["free_months", "monthly_discount", "one_time_credit", "other"]
    monthly_equivalent: Money = Field(ge=Decimal("0"))  # computed by code, never by the LLM
    months_free: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("24"))
    monthly_discount: Money | None = Field(default=None, ge=Decimal("0"))
    one_time_credit: Money | None = Field(default=None, ge=Decimal("0"))
    lease_term_months: int | None = Field(default=None, ge=1, le=60)
    raw_text: str
    grounded: bool  # every LLM number found in raw_text
    citation: Citation


class RentRollUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1)
    unit_type: str | None = None
    beds: int | None = Field(default=None, ge=0, le=6)
    baths: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("6"))
    sq_ft: int | None = Field(default=None, gt=0, lt=10_000)
    status: Status
    in_place_rent: Money | None = Field(default=None, ge=Decimal("0"))  # $0 allowed on purpose
    market_rent: Money | None = Field(default=None, ge=Decimal("0"))
    lease_start: date | None = None
    lease_end: date | None = None
    concessions: list[Concession] = []
    audit_flags: list[str] = []
    citations: dict[str, Citation]  # field name -> source; "unit_id" required

    @model_validator(mode="after")
    def _checks(self) -> RentRollUnit:
        if "unit_id" not in self.citations:
            raise ValueError(f"{self.unit_id}: missing unit_id citation")
        if self.lease_start and self.lease_end and self.lease_end < self.lease_start:
            raise ValueError(f"{self.unit_id}: lease_end before lease_start")
        return self


class SubtotalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    column: Literal["in_place_rent", "market_rent"]
    scope: Literal["page", "unit_type", "grand_total"]
    stated_value: Money
    covers_unit_ids: list[str]  # units assigned to this block
    citation: Citation


class ColumnMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header_text: str
    maps_to: RRField


class HeaderMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[ColumnMap]
    source: Literal["synonym_table", "llm", "manual_override"]

    @model_validator(mode="after")
    def _one_to_one(self) -> HeaderMap:
        used = [c.maps_to for c in self.columns if c.maps_to != "ignore"]
        dupes = sorted({f for f in used if used.count(f) > 1})
        missing = sorted({"unit_id", "in_place_rent"} - set(used))
        no_status = "status" not in used and "tenant_name" not in used
        problems = []
        if dupes:
            problems.append(f"mapped more than once: {dupes}")
        if missing:
            problems.append(f"missing required: {missing}")
        if no_status:
            problems.append("need a 'status' or 'tenant_name' column")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RentRoll(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_name: str | None = None
    as_of_date: date | None = None
    source_file: str
    source_format: SourceFormat
    header_map: HeaderMap
    units: list[RentRollUnit]
    stated_totals: dict[Literal["in_place_rent", "market_rent"], Money] = {}
    stated_total_citations: dict[Literal["in_place_rent", "market_rent"], Citation] = {}
    subtotals: list[SubtotalRow] = []
    extraction_warnings: list[str] = []

    @model_validator(mode="after")
    def _totals_cited(self) -> RentRoll:
        if set(self.stated_totals) != set(self.stated_total_citations):
            raise ValueError("every stated total needs a citation")
        return self
