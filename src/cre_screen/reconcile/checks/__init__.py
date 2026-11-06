"""Check registry in SPEC 8.3 order. Built so far: RR_FOOTING (M2), T12_FOOTING (M3, pulled
forward from M4 at the owner's request). M4 adds the remaining 13."""

from __future__ import annotations

from cre_screen.reconcile.checks import rr_footing, t12_footing
from cre_screen.reconcile.context import Check
from cre_screen.schemas.common import CheckId

REGISTRY: dict[CheckId, Check] = {
    "RR_FOOTING": rr_footing.check,
    "T12_FOOTING": t12_footing.check,
}
