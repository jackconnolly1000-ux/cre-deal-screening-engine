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
    action: Literal["clear", "override", "edit_value", "remap_line"]
    target: str  # unit_id, line label, or "*"
    note: str = Field(min_length=10)
    at: datetime
