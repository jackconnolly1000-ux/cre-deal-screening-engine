from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .citation import Citation
from .common import COABucket, Money, SourceFormat


class T12Line(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    bucket: COABucket
    monthly: list[Money] = Field(min_length=12, max_length=12)
    stated_total: Money | None = None
    is_nonrecurring: bool = False
    mapping_source: Literal["synonym_table", "fuzzy", "llm", "manual_override"]
    mapping_confidence: float = Field(ge=0.0, le=1.0)  # derived from route, never LLM-reported
    audit_flags: list[str] = []
    citation: Citation  # the row
    cell_citations: dict[str, Citation] = {}  # "m01".."m12", "total"

    @property
    def include_in_uw(self) -> bool:
        # Plain property, not @computed_field: a computed field is serialized and then
        # rejected by extra="forbid" on the way back in, breaking JSON round-trips.
        return not self.is_nonrecurring and self.bucket not in ("NON_OPERATING", "UNMAPPED")

    def computed_total(self) -> Decimal:
        return sum(self.monthly, Decimal("0"))

    def t3_annualized(self) -> Decimal:
        return sum(self.monthly[9:12], Decimal("0")) * 4


class StatedSubtotal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    kind: Literal["total_income", "total_expenses", "noi", "other"]
    monthly: list[Money | None] = Field(min_length=12, max_length=12)
    total: Money | None = None
    citation: Citation


class T12Statement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_name: str | None = None
    period_end: date  # last day of month 12
    month_labels: list[str] = Field(min_length=12, max_length=12)
    source_file: str
    source_format: SourceFormat
    lines: list[T12Line]
    stated_subtotals: list[StatedSubtotal] = []
    extraction_warnings: list[str] = []

    @model_validator(mode="after")
    def _unique_labels(self) -> T12Statement:
        labels = [ln.label for ln in self.lines]
        if len(labels) != len(set(labels)):
            raise ValueError("duplicate T-12 line labels; extractor must suffix duplicates ' (2)'")
        return self

    def bucket_total(self, bucket: str, *, uw_only: bool = False) -> Decimal:
        return sum(
            (
                ln.computed_total()
                for ln in self.lines
                if ln.bucket == bucket and (ln.include_in_uw or not uw_only)
            ),
            Decimal("0"),
        )

    def bucket_t3_annualized(self, bucket: str, *, uw_only: bool = False) -> Decimal:
        return sum(
            (
                ln.t3_annualized()
                for ln in self.lines
                if ln.bucket == bucket and (ln.include_in_uw or not uw_only)
            ),
            Decimal("0"),
        )
