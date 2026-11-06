from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import Provenance


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_file: str  # relative to the deal folder, "/" separators
    page: int | None = Field(default=None, ge=1)  # PDF, 1-based
    sheet: str | None = None  # Excel
    cell: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[1-9][0-9]*$")
    row_index: int = Field(ge=0)  # row index within the extracted table
    bbox: tuple[float, float, float, float] | None = None  # x0, top, x1, bottom in PDF points
    raw_text: str  # exactly as printed, before parsing
    provenance: Provenance

    @model_validator(mode="after")
    def _has_location(self) -> Citation:
        if self.provenance == "manual_entry":
            return self
        if self.page is None and self.cell is None:
            raise ValueError("citation needs a PDF page or an Excel cell")
        if self.cell is not None and self.sheet is None:
            raise ValueError("an Excel cell citation needs a sheet name")
        return self

    def short(self) -> str:
        """Human-readable location, e.g. 'rent_roll.pdf p.3 row 14' or 't12.xlsx T12!F22'."""
        if self.page is not None:
            return f"{self.source_file} p.{self.page} row {self.row_index}"
        if self.cell is not None:
            return f"{self.source_file} {self.sheet}!{self.cell}"
        return f"{self.source_file} (manual entry)"
