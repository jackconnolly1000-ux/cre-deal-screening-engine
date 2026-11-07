from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .citation import Citation
from .common import CheckId, Money, Severity


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: CheckId
    severity: Severity
    message: str
    delta: Money | None = None
    unit_ids: list[str] = []
    line_labels: list[str] = []
    citations: list[Citation] = []
    details: dict[str, str] = {}  # e.g. {"cause": "document"}
    status: Literal["open", "cleared", "overridden"] = "open"
    reviewer_note: str | None = None


class ExpectedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: CheckId
    trap_id: str
    unit_ids: list[str] = []
    line_labels: list[str] = []


GateState = Literal["FOOTING", "REEXTRACT", "CHECKS", "REVIEW", "READY"]


class GateTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_state: GateState
    to_state: GateState
    reason: str
    at: datetime


class Override(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: CheckId
    action: Literal["clear", "override", "edit_value", "remap_line", "resolve_footing"]
    # unit_id, line label or "*" for clear/override; "<unit id>:<field>" for edit_value;
    # "<line label>=<BUCKET>" for remap_line; the stated-total column for resolve_footing
    target: str
    note: str = Field(min_length=10)
    at: datetime
    corrected_value: Money | None = None  # the number that replaces the broken one
    resolution_strategy: (
        Literal["use_calculated_sum", "use_stated_total", "edit_line_item"] | None
    ) = None
