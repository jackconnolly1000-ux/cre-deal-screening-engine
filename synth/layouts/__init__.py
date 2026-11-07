"""Layout families (SPEC 11.3). Registries map the names used in plan.py to their modules."""

from __future__ import annotations

from synth.layouts import (
    rr_classic,
    rr_excel,
    rr_grouped,
    rr_wide,
    t12_excel,
    t12_grid,
    t12_sections,
)

RR = {
    "rr_classic": rr_classic,
    "rr_grouped": rr_grouped,
    "rr_wide": rr_wide,
    "rr_excel": rr_excel,
}
T12 = {"t12_grid": t12_grid, "t12_sections": t12_sections, "t12_excel": t12_excel}

# Column sets that bound header widths (the Excel layout has no widths; it uses the classic set).
RR_COLUMNS = {
    "rr_classic": rr_classic.COLUMNS,
    "rr_grouped": rr_grouped.COLUMNS,
    "rr_wide": rr_wide.COLUMNS,
    "rr_excel": rr_classic.COLUMNS,
}
