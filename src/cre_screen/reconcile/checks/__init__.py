"""Check registry, in the fixed order of SPEC 8.3. One check per module."""

from __future__ import annotations

from cre_screen.reconcile.checks import (
    pf_ins_drop,
    pf_mgmt_fee,
    pf_tax_drop,
    pf_vacancy,
    rr_concession,
    rr_dup_unit,
    rr_expired_lease,
    rr_footing,
    rr_rent_outlier,
    rr_zero_rent_occ,
    t12_footing,
    t12_nonrecurring,
    t12_spike,
    t12_unmapped,
    x_rr_vs_t3,
)
from cre_screen.reconcile.context import Check
from cre_screen.schemas.common import CheckId

REGISTRY: dict[CheckId, Check] = {
    "RR_FOOTING": rr_footing.check,
    "RR_DUP_UNIT": rr_dup_unit.check,
    "RR_ZERO_RENT_OCC": rr_zero_rent_occ.check,
    "RR_EXPIRED_LEASE": rr_expired_lease.check,
    "RR_CONCESSION": rr_concession.check,
    "RR_RENT_OUTLIER": rr_rent_outlier.check,
    "X_RR_VS_T3": x_rr_vs_t3.check,
    "T12_FOOTING": t12_footing.check,
    "T12_SPIKE": t12_spike.check,
    "T12_NONRECURRING": t12_nonrecurring.check,
    "T12_UNMAPPED": t12_unmapped.check,
    "PF_TAX_DROP": pf_tax_drop.check,
    "PF_INS_DROP": pf_ins_drop.check,
    "PF_VACANCY": pf_vacancy.check,
    "PF_MGMT_FEE": pf_mgmt_fee.check,
}
