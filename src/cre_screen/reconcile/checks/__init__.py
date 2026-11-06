"""Check registry. M2 ships RR_FOOTING; M4 adds the other 14 in the SPEC 8.3 order."""

from __future__ import annotations

from cre_screen.reconcile.checks import rr_footing
from cre_screen.reconcile.context import Check
from cre_screen.schemas.common import CheckId

REGISTRY: dict[CheckId, Check] = {
    "RR_FOOTING": rr_footing.check,
}
