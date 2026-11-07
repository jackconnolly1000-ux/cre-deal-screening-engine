"""Which layout family and file format each synthetic deal uses (SPEC 11.2 and 11.3).

Format counts the SPEC requires: dev has 4 xlsx rent rolls, 2 scanned rent rolls and 4 xlsx
T-12s; held-out has 2, 2 and 2. Deals 001 to 005 stay classic/grid so their committed files do
not change. The duplicate-row traps need fixed-size pages, so every deal that carries one uses
``rr_classic``.
"""

from __future__ import annotations

from dataclasses import dataclass

RR_LAYOUTS = ("rr_classic", "rr_grouped", "rr_wide", "rr_excel")
T12_LAYOUTS = ("t12_grid", "t12_sections", "t12_excel")


@dataclass(frozen=True)
class LayoutPlan:
    rr_layout: str
    t12_layout: str
    scanned: bool = False

    @property
    def rr_format(self) -> str:
        if self.rr_layout == "rr_excel":
            return "xlsx"
        return "pdf_scanned" if self.scanned else "pdf_text"

    @property
    def t12_format(self) -> str:
        return "xlsx" if self.t12_layout == "t12_excel" else "pdf_text"


C, G, W, X = "rr_classic", "rr_grouped", "rr_wide", "rr_excel"
GRID, SEC, XL = "t12_grid", "t12_sections", "t12_excel"

PLAN: dict[int, LayoutPlan] = {
    # dev
    1: LayoutPlan(C, GRID),
    2: LayoutPlan(C, GRID),
    3: LayoutPlan(C, GRID),
    4: LayoutPlan(C, GRID),
    5: LayoutPlan(C, GRID),
    6: LayoutPlan(G, SEC),
    7: LayoutPlan(W, SEC),
    8: LayoutPlan(X, XL),
    9: LayoutPlan(X, XL),
    10: LayoutPlan(C, SEC, scanned=True),
    11: LayoutPlan(C, GRID, scanned=True),
    12: LayoutPlan(X, XL),
    13: LayoutPlan(X, SEC),
    14: LayoutPlan(C, XL),
    # held-out
    15: LayoutPlan(C, GRID),
    16: LayoutPlan(C, SEC, scanned=True),
    17: LayoutPlan(X, XL),
    18: LayoutPlan(G, GRID, scanned=True),
    19: LayoutPlan(X, SEC),
    20: LayoutPlan(C, XL),
}

DEV_DEALS = tuple(range(1, 15))
HELDOUT_DEALS = tuple(range(15, 21))
