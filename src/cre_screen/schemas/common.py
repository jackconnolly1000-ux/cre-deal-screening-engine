from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, get_args

from pydantic import Field

Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]
Rate = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1"), max_digits=9, decimal_places=6)]

CENT = Decimal("0.01")


def to_cents(value: Decimal) -> Decimal:
    """Quantize to cents with ROUND_HALF_UP. The only rounding function allowed for money."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


Provenance = Literal["text_layer", "excel", "llm_vision", "manual_entry", "manual_override"]
Severity = Literal["block", "warn", "info"]
SourceFormat = Literal["pdf_text", "pdf_scanned", "xlsx"]

Status = Literal["occupied", "vacant", "notice", "model", "down", "employee", "unknown"]

RRField = Literal[
    "unit_id",
    "unit_type",
    "beds",
    "baths",
    "sq_ft",
    "status",
    "tenant_name",
    "in_place_rent",
    "market_rent",
    "lease_start",
    "lease_end",
    "concession_note",
    "ignore",
]

COABucket = Literal[
    # revenue (deductions stored negative)
    "GROSS_POTENTIAL_RENT",
    "LOSS_TO_LEASE",
    "VACANCY_LOSS",
    "CONCESSIONS",
    "BAD_DEBT",
    "UTILITY_REIMBURSEMENT",
    "OTHER_INCOME",
    # operating expenses
    "REAL_ESTATE_TAXES",
    "INSURANCE",
    "UTILITIES",
    "REPAIRS_MAINTENANCE",
    "TURNOVER",
    "CONTRACT_SERVICES",
    "PAYROLL",
    "MANAGEMENT_FEE",
    "GENERAL_ADMIN",
    "MARKETING",
    # control buckets
    "CAPEX_RESERVES",
    "NON_OPERATING",
    "UNMAPPED",
]

REVENUE_BUCKETS: tuple[str, ...] = get_args(COABucket)[0:7]
OPEX_BUCKETS: tuple[str, ...] = get_args(COABucket)[7:17]
CONTROL_BUCKETS: tuple[str, ...] = get_args(COABucket)[17:20]
RENTAL_BUCKETS: tuple[str, ...] = (
    "GROSS_POTENTIAL_RENT",
    "LOSS_TO_LEASE",
    "VACANCY_LOSS",
    "CONCESSIONS",
    "BAD_DEBT",
)

CheckId = Literal[
    "RR_FOOTING",
    "RR_DUP_UNIT",
    "RR_ZERO_RENT_OCC",
    "RR_EXPIRED_LEASE",
    "RR_CONCESSION",
    "RR_RENT_OUTLIER",
    "X_RR_VS_T3",
    "T12_FOOTING",
    "T12_SPIKE",
    "T12_NONRECURRING",
    "T12_UNMAPPED",
    "PF_TAX_DROP",
    "PF_INS_DROP",
    "PF_VACANCY",
    "PF_MGMT_FEE",
]
