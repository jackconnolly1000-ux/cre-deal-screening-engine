from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import COABucket, RRField


class LLMColumnMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header_text: str
    maps_to: RRField


class LLMHeaderMap(BaseModel):
    """Converted to rent_roll.HeaderMap(source='llm'); its validator enforces one-to-one."""

    model_config = ConfigDict(extra="forbid")
    columns: list[LLMColumnMap]


class COAMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_label: str
    bucket: COABucket
    is_nonrecurring: bool
    rationale: str = Field(max_length=160)


class COAMappingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[COAMapping]


class ConcessionParse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["free_months", "monthly_discount", "one_time_credit", "other"]
    months_free: Decimal | None = None
    monthly_discount: Decimal | None = None
    one_time_credit: Decimal | None = None
    lease_term_months: int | None = None


class VisionRow(BaseModel):
    """Strings exactly as printed. Code parses them. Empty string if the cell is blank."""

    model_config = ConfigDict(extra="forbid")
    cells: dict[str, str]  # header_text -> printed text
    row_kind: Literal["unit", "subtotal", "header", "footnote", "other"]


class VisionTablePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headers: list[str]
    rows: list[VisionRow]
