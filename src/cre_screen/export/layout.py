"""Every row, column and name in the workbook (SPEC section 9). Nothing else hard-codes them."""

from __future__ import annotations

from typing import Final

from cre_screen.schemas.common import OPEX_BUCKETS, RENTAL_BUCKETS, REVENUE_BUCKETS

# ---------- tabs, in workbook order ----------

README: Final = "README"
ASSUMPTIONS: Final = "Assumptions"
AUDIT: Final = "Audit_Inputs"
RENT_ROLL: Final = "Rent_Roll"
T12: Final = "T12_Mapped"
FINDINGS: Final = "Findings"
PROFORMA: Final = "ProForma_NOI_DSCR"
SHEET_ORDER: Final = (README, ASSUMPTIONS, AUDIT, RENT_ROLL, T12, FINDINGS, PROFORMA)

AUTHOR: Final = "cre-screen"
TITLE: Final = "cre-screen underwriting export"
SYNTHETIC_NOTE: Final = "Synthetic test data; not a real property."
LEGEND: Final = (
    "Blue = input. Black = formula. Thresholds are placeholders; see config/thresholds.yaml."
)

# ---------- number formats and colors ----------

MONEY: Final = "#,##0.00;(#,##0.00)"
PERCENT: Final = "0.00%"
RATIO: Final = '0.00"x"'
DATE: Final = "yyyy-mm-dd"
BLUE: Final = "0000FF"
BLACK: Final = "000000"

# ---------- bounded row ranges (no whole-column references anywhere) ----------

RR_FIRST: Final = 2
RR_LAST: Final = 2001
T12_FIRST: Final = 2
T12_LAST: Final = 201
PF_FIRST: Final = 21
PF_LAST: Final = 40

RR_TABLE_NAME: Final = "RentRollTable"
RR_COLUMNS: Final = (
    ("A", "Unit"),
    ("B", "Unit Type"),
    ("C", "Beds"),
    ("D", "Baths"),
    ("E", "SqFt"),
    ("F", "Status"),
    ("G", "In-Place Rent"),
    ("H", "Market Rent"),
    ("I", "Lease Start"),
    ("J", "Lease End"),
    ("K", "Concession Monthly Equiv"),
    ("L", "Effective Rent"),
    ("M", "Flags"),
    ("N", "Source"),
)
RR_LAST_COLUMN: Final = "N"

T12_MONTH_COLUMNS: Final = tuple("EFGHIJKLMNOP")
T12_COLUMNS: Final = (
    ("A", "Label"),
    ("B", "Bucket"),
    ("C", "Include_In_UW"),
    ("D", "Nonrecurring"),
    ("Q", "Total"),
    ("R", "Stated Total"),
    ("S", "T3_Annualized"),
    ("T", "Mapping Source"),
    ("U", "Source"),
)

FINDINGS_COLUMNS: Final = (
    "Check",
    "Severity",
    "Status",
    "Message",
    "Delta",
    "Units / Lines",
    "Citations",
    "Reviewer Note",
)
AUDIT_COLUMNS: Final = ("Item", "Value", "Source file", "Location", "Raw text", "Provenance")
SEVERITY_RANK: Final = {"block": 0, "warn": 1, "info": 2}

# ---------- Assumptions ----------

# Workbook-scoped names, one per row 2..17: (name, row)
ASSUMPTION_NAMES: Final = (
    ("AskingPrice", 2),
    ("EntryCapRate", 3),
    ("PurchasePrice", 4),
    ("LTV", 5),
    ("LoanAmount", 6),
    ("InterestRate", 7),
    ("AmortYears", 8),
    ("IOPeriodMonths", 9),
    ("ExpenseGrowth", 10),
    ("MgmtFeePct", 11),
    ("ReservesPerUnit", 12),
    ("UnitCount", 13),
    ("AssessmentRatio", 14),
    ("TaxRate", 15),
    ("ReassessedTaxBump", 16),
    ("ReassessedTax", 17),
)
ASSUMPTION_ROW: Final = dict(ASSUMPTION_NAMES)
PF_HEADER_ROW: Final = 20

ASSUMPTION_NOTES: Final = {
    "AskingPrice": "From proforma.yaml",
    "EntryCapRate": "Used only if no asking price",
    "PurchasePrice": "Asking price, else broker NOI / entry cap rate",
    "LTV": "",
    "LoanAmount": "",
    "InterestRate": "",
    "AmortYears": "",
    "IOPeriodMonths": "Year-1 debt service blends IO and amortizing months",
    "ExpenseGrowth": "Applied to T-12 opex in underwritten column",
    "MgmtFeePct": "Floor on management fee as % of EGI",
    "ReservesPerUnit": "Placeholder; calibrate",
    "UnitCount": "Rent roll rows",
    "AssessmentRatio": "Local assessment ratio, if known",
    "TaxRate": "Local tax rate as a decimal, if known",
    "ReassessedTaxBump": "Fallback only; arbitrary placeholder",
    "ReassessedTax": "Reassessed at purchase price if ratio and rate are known",
}
ASSUMPTION_FORMATS: Final = {
    "AskingPrice": MONEY,
    "EntryCapRate": PERCENT,
    "PurchasePrice": MONEY,
    "LTV": PERCENT,
    "LoanAmount": MONEY,
    "InterestRate": PERCENT,
    "AmortYears": "0",
    "IOPeriodMonths": "0",
    "ExpenseGrowth": PERCENT,
    "MgmtFeePct": PERCENT,
    "ReservesPerUnit": MONEY,
    "UnitCount": "0",
    "AssessmentRatio": "0.0000",
    "TaxRate": "0.0000",
    "ReassessedTaxBump": "0.00",
    "ReassessedTax": MONEY,
}
ASSUMPTION_FORMULAS: Final = {
    "PurchasePrice": "=IF(AskingPrice>0,AskingPrice,BrokerNOI/EntryCapRate)",
    "LoanAmount": "=PurchasePrice*LTV",
    "UnitCount": f"=COUNTA({RENT_ROLL}!$A${RR_FIRST}:$A${RR_LAST})",
    "ReassessedTax": (
        "=IF(AND(AssessmentRatio>0,TaxRate>0),PurchasePrice*AssessmentRatio*TaxRate,"
        "T12Taxes*ReassessedTaxBump)"
    ),
}

# Names defined on other tabs
BROKER_NOI_NAME: Final = "BrokerNOI"
T12_TAXES_NAME: Final = "T12Taxes"

# ---------- ProForma_NOI_DSCR ----------

PF_COLUMNS: Final = ("B", "C", "D", "E")  # T-12, T-3 annualized, broker, underwritten
PF_HEADER_LABELS: Final = (
    "Line",
    "T-12 Actual",
    "T-3 Annualized",
    "Broker Pro Forma",
    "Underwritten",
    "Notes",
)
PF_HEADER_ROW_NUMBER: Final = 2

ROW_REVENUE_LABEL: Final = 3
ROW_EGI: Final = 11
ROW_OPEX_LABEL: Final = 12
ROW_OPEX_TOTAL: Final = 23
ROW_NOI: Final = 24
ROW_RESERVES: Final = 25
ROW_NCF: Final = 26
ROW_VALUATION_LABEL: Final = 27
ROW_PRICE: Final = 28
ROW_CAP_RATE: Final = 29
ROW_LOAN: Final = 30
ROW_DEBT_SERVICE: Final = 31
ROW_DSCR: Final = 32
ROW_DEBT_YIELD: Final = 33
ROW_OPERATIONS_LABEL: Final = 34
ROW_PHYS_OCC: Final = 35
ROW_ECON_OCC: Final = 36
ROW_LTL: Final = 37
PF_FIRST_ROW: Final = 4
PF_LAST_ROW: Final = 37

REVENUE_ROWS: Final = {b: 4 + i for i, b in enumerate(REVENUE_BUCKETS)}  # rows 4..10
OPEX_ROWS: Final = {b: 13 + i for i, b in enumerate(OPEX_BUCKETS)}  # rows 13..22
SECTION_LABEL_ROWS: Final = {
    ROW_REVENUE_LABEL: "Revenue",
    ROW_OPEX_LABEL: "Operating expenses",
    ROW_VALUATION_LABEL: "Valuation and debt",
    ROW_OPERATIONS_LABEL: "Operations",
}
SUMMARY_LABELS: Final = {
    ROW_EGI: "Effective Gross Income",
    ROW_OPEX_TOTAL: "Total Operating Expenses",
    ROW_NOI: "Net Operating Income",
    ROW_RESERVES: "Replacement Reserves",
    ROW_NCF: "Net Cash Flow",
    ROW_PRICE: "Purchase Price",
    ROW_CAP_RATE: "Cap Rate on NOI",
    ROW_LOAN: "Loan Amount",
    ROW_DEBT_SERVICE: "Debt Service (Year 1)",
    ROW_DSCR: "DSCR (on NCF)",
    ROW_DEBT_YIELD: "Debt Yield",
    ROW_PHYS_OCC: "Physical Occupancy",
    ROW_ECON_OCC: "Economic Occupancy",
    ROW_LTL: "Monthly Loss to Lease",
}
ROW_FORMATS: Final = {
    ROW_CAP_RATE: PERCENT,
    ROW_DSCR: RATIO,
    ROW_DEBT_YIELD: PERCENT,
    ROW_PHYS_OCC: PERCENT,
    ROW_ECON_OCC: PERCENT,
}
ROW_NOTES: Final = {
    ROW_EGI: "Sum of revenue lines",
    13: "Greater of T-12 taxes and reassessed taxes (see Assumptions)",
    14: "T-12 recurring, grown by ExpenseGrowth",
    20: "Greater of grown T-12 fee and MgmtFeePct x EGI",
    ROW_RESERVES: "Underwritten: ReservesPerUnit x UnitCount",
    ROW_DEBT_SERVICE: "Blends interest-only and amortizing months",
    ROW_PHYS_OCC: "Rent roll, same in every column",
    ROW_LTL: "Monthly, occupied and notice units",
}
RENTAL_ROW_BUCKETS: Final = RENTAL_BUCKETS  # rows whose underwritten column uses T-3 annualized

# Absolute ranges used inside formulas
Q_RANGE: Final = f"{T12}!$Q${T12_FIRST}:$Q${T12_LAST}"
S_RANGE: Final = f"{T12}!$S${T12_FIRST}:$S${T12_LAST}"
BKT_RANGE: Final = f"{T12}!$B${T12_FIRST}:$B${T12_LAST}"
INC_RANGE: Final = f"{T12}!$C${T12_FIRST}:$C${T12_LAST}"
PFV_RANGE: Final = f"{ASSUMPTIONS}!$B${PF_FIRST}:$B${PF_LAST}"
PFB_RANGE: Final = f"{ASSUMPTIONS}!$A${PF_FIRST}:$A${PF_LAST}"
RR_STATUS_RANGE: Final = f"{RENT_ROLL}!$F${RR_FIRST}:$F${RR_LAST}"
RR_MARKET_RANGE: Final = f"{RENT_ROLL}!$H${RR_FIRST}:$H${RR_LAST}"
RR_RENT_RANGE: Final = f"{RENT_ROLL}!$G${RR_FIRST}:$G${RR_LAST}"
