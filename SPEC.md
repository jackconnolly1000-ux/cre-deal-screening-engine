# SPEC.md: CRE Deal-Screening & Reconciliation Engine (`cre-screen`)

Version 1.0, 2026-11-06. Owner: Jack Connolly. Repo: `jackconnolly1000-ux/cre-screen` (public).

This file is the build contract. Claude Code builds against it one milestone at a time (Section 12). If code and this spec disagree, the spec wins until the owner edits the spec. If the spec is ambiguous, stop and ask the owner; do not guess.

---

## 1. Product summary

`cre-screen` takes a multifamily deal's rent roll and T-12 operating statement (PDF or Excel) plus a broker pro forma typed into `proforma.yaml`. It extracts every number with a page/row or sheet/cell citation, maps T-12 lines to a fixed chart of accounts, runs a deterministic reconciliation gate that flags broker errors and underwriting red flags, and exports a source-cited Excel workbook whose NOI and DSCR tab is 100% live formulas.

The project's claim is not "AI extracts rent rolls." Commercial tools already do that. The claim is: a zero-trust reconciliation layer with a published, reproducible benchmark showing what it catches and what it misses.

### 1.1 v1 scope

- Rent roll: text-layer PDF, scanned PDF (vision fallback), Excel (.xlsx).
- T-12: text-layer PDF, Excel (.xlsx). A text-layer T-12 that the table reader cannot read falls back to the vision reader (Section 7.9); a T-12 that is a scan from the start is read the same way.
- Broker pro forma: typed by hand into `proforma.yaml` per deal.
- 15 deterministic checks (Section 8).
- Excel export with 7 tabs (Section 9).
- Streamlit review UI (Section 10).
- Synthetic benchmark: 14 dev deals + 6 locked held-out deals, plus 3 real public deals scored by hand (Section 11).

### 1.2 Non-goals (v2 or never)

- Offering Memorandum (OM) extraction: marketing prose, photos, or OM pro forma tables. v2.
- Scanned T-12 PDFs. v2.
- Legacy `.xls` files. Convert to `.xlsx` by hand.
- Property types other than multifamily.
- Multi-user accounts, hosting, authentication, databases.
- Market data, comps, rent forecasts, IRR or waterfall modeling.
- Any LLM "critic" or agent framework (LangGraph, CrewAI, LlamaIndex, vector DB).
- Local LLMs.

---

## 2. Design invariants (never violate)

1. **The LLM never produces a number that lands in a typed record**, with exactly two exceptions:
   - Vision path: the LLM transcribes text from a scanned page as **strings**; code parses them. Every such value carries `provenance="llm_vision"`.
   - Concession parsing: the LLM extracts quantities from a free-text note. Every number it returns must appear literally in the note's text (grounding check, Section 7.5). Failure means the concession goes to review, not into the record.
2. **Money is `decimal.Decimal`, quantized to cents.** `float` is forbidden for money anywhere in `src/`, `synth/`, `evals/`. The only float conversion allowed is inside `export/` when openpyxl writes a cell, and inside the recalculation test.
3. **Every extracted value has a `Citation`.** No citation, no record.
4. **The reconciliation gate is deterministic Python.** Same input, same findings, every run. No LLM calls inside `reconcile/`.
5. **Every threshold lives in `config/thresholds.yaml`.** No magic numbers in check code.
6. **Schemas enforce shape; checks enforce business rules.** A $0-rent occupied unit must load successfully and then be flagged. Validators reject malformed data, never suspicious data.
7. **Tests never call a live API.** Unit tests use `FakeLLMClient`. Evals use the replay cache. Only tests marked `@pytest.mark.live` may call the API, and they are skipped by default.
8. **Held-out deals are locked.** No code under `src/` may be changed in response to held-out results before the final run is recorded (Section 11.6).
9. **Windows-native.** `pathlib.Path` everywhere, `encoding="utf-8"` on every text read and write, `newline=""` for CSV, no shell scripts, no symlinks, no POSIX-only calls.

---

## 3. Environment

- Python 3.11 or 3.12 (native Windows 11; CI also runs Ubuntu).
- Package manager: `uv`. Lockfile `uv.lock` is committed.
- Editor: VS Code, PowerShell terminal.
- Hardware assumption: 16 GB RAM, no GPU. Nothing in the pipeline may need a GPU.

### 3.1 Dependencies (`pyproject.toml`)

Runtime:

| Package | Purpose |
| --- | --- |
| `pydantic>=2.7` | Schemas |
| `pyyaml` | Config and `proforma.yaml` |
| `anthropic` | Claude API |
| `instructor` | Structured outputs with validate-and-retry |
| `pdfplumber` | Text-layer PDF words and coordinates |
| `pymupdf` | Page rendering, highlight boxes, rasterizing test scans (AGPL; wrapped in `ingest/render.py`) |
| `openpyxl` | Excel read and write |
| `rapidfuzz` | Fuzzy header and label matching |
| `reportlab` | Synthetic PDF generation |
| `faker` | Synthetic names |
| `pillow` | Scan degradation |
| `streamlit` | Review UI |
| `formulas` | Headless Excel recalculation in tests (verified on 1.3.4: bounded SUMIFS, named ranges, IF, AND, MIN, MAX, PMT, IFERROR, COUNTIFS, COUNTA) |

Dev: `pytest`, `pytest-cov`, `hypothesis`, `ruff`.

### 3.2 `pyproject.toml` tool settings

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "RUF"]

[tool.pytest.ini_options]
pythonpath = ["src", "."]
testpaths = ["tests", "evals"]
addopts = "-m 'not live and not heldout'"
markers = [
  "live: calls the real Anthropic API; costs money; never in CI",
  "heldout: touches locked held-out deals; only during the M8 final run",
  "slow: takes more than 10 seconds",
]
```

### 3.3 Environment variables

| Variable | Meaning | Default |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | API key. Never committed. Loaded from the shell or a gitignored `.env`. | none |
| `CRE_LLM_CACHE_MODE` | Overrides `cache.mode` in `models.yaml`: `replay`, `record`, `off` | unset |
| `PYTHONUTF8` | Set to `1` in the owner's PowerShell profile and in CI | `1` in CI |

---

## 4. Repository layout

```text
cre-screen/
  pyproject.toml
  uv.lock
  README.md
  SPEC.md
  CLAUDE.md
  LICENSE                       # AGPL-3.0 (because PyMuPDF is AGPL); see README note
  .gitignore                    # data/real/, out/, .env, __pycache__, .venv, *.pyc
  .cost_ledger.jsonl            # committed; one line per live API attempt
  .github/workflows/ci.yml
  config/
    coa.yaml                    # 20 buckets, descriptions, synonyms, sign, nonrecurring keywords
    rr_headers.yaml             # rent roll field synonyms, status synonyms
    thresholds.yaml             # every check threshold
    models.yaml                 # model IDs, prices, token limits, budget caps, cache mode
  src/cre_screen/
    __init__.py
    __main__.py                 # CLI entry point (Section 13)
    config.py                   # loads and validates the four YAML files into Pydantic models
    errors.py                   # every custom exception (Section 7), one base class CreScreenError
    pipeline.py                 # run_deal(deal_dir) -> DealPackage
    schemas/
      __init__.py
      common.py
      citation.py
      rent_roll.py
      t12.py
      proforma.py
      mapping.py                # LLM-facing response models
      findings.py
      deal.py
    ingest/
      __init__.py
      router.py                 # file and page type detection
      parse.py                  # parse_money, parse_date, parse_int, parse_decimal, normalize_status
      pdf_tables.py             # pdfplumber words -> RawTable
      excel_tables.py           # openpyxl -> RawTable
      raw.py                    # RawCell, RawRow, RawTable dataclasses
      render.py                 # ONLY module that imports fitz (PyMuPDF)
    mapping/
      __init__.py
      llm_client.py             # LLMClient, the only module that imports anthropic/instructor
      cache.py                  # LLMCache
      cost.py                   # CostTracker, ledger
      header_mapper.py
      coa_mapper.py
      concessions.py
      vision.py
      prompts/
        header_map.md
        coa_map.md
        concession.md
        vision_page.md
        vision_t12.md
    extract/
      __init__.py
      rent_roll.py              # RawTable + HeaderMap -> RentRoll
      t12.py                    # RawTable -> T12Statement
      proforma.py               # proforma.yaml -> BrokerProForma
    reconcile/
      __init__.py
      gate.py                   # state machine
      context.py                # CheckContext
      reread.py                 # independent re-read for footing localization
      metrics.py                # occupancy, loss to lease, t3 helpers (pure functions)
      checks/
        __init__.py             # REGISTRY in fixed order
        rr_footing.py
        rr_dup_unit.py
        rr_zero_rent_occ.py
        rr_expired_lease.py
        rr_concession.py
        rr_rent_outlier.py
        x_rr_vs_t3.py
        t12_footing.py
        t12_spike.py
        t12_nonrecurring.py
        t12_unmapped.py
        pf_tax_drop.py
        pf_ins_drop.py
        pf_vacancy.py
        pf_mgmt_fee.py
    export/
      __init__.py
      excel_writer.py
      layout.py                 # every row/column constant for the workbook
      uw_math.py                # pure-Python Decimal mirror of the workbook math
    app/
      __init__.py
      streamlit_app.py
  synth/
    __init__.py
    generate.py                 # CLI: python -m synth.generate
    params/
      base.yaml                 # distributions; owner fills from Philadelphia metrics
    truth.py                    # build DealTruth
    traps.py                    # trap classes
    layouts/
      __init__.py
      rr_classic.py
      rr_grouped.py
      rr_wide.py
      rr_excel.py
      t12_grid.py
      t12_sections.py
      t12_excel.py
    degrade.py                  # scanned-page simulation
  data/
    real/                       # gitignored; 3 public deals + manual truth
    synthetic/
      deal_001/ ... deal_020/
      HELDOUT_MANIFEST.sha256
  evals/
    __init__.py
    run_eval.py                 # CLI: python -m evals.run_eval
    scoring.py
    report.py
    test_regression.py
    baseline_dev.json
    manual_timing.yaml
    llm_cache/                  # committed replay cache
    reports/
  tests/
    conftest.py
    fakes.py                    # FakeLLMClient
    fixtures/
    (test files listed per milestone in Section 12)
  out/                          # gitignored; exported workbooks
```

### 4.1 Deal folder contents

```text
data/synthetic/deal_007/
  rent_roll.pdf | rent_roll.xlsx     # exactly one
  t12.pdf | t12.xlsx                 # exactly one
  proforma.yaml
  truth.json                         # synthetic only
  meta.json                          # seed, split, layout families, scanned flag, traps
```

Real deals in `data/real/<name>/` hold the same files except `truth.json`, replaced by `truth_manual.yaml` (Section 11.5).

---

## 5. Configuration files

All four files load through `src/cre_screen/config.py` into Pydantic models with `extra="forbid"`. A typo in a key is a startup error.

### 5.1 `config/coa.yaml`

Revenue deductions are stored **negative**. Expenses are stored **positive**. If a document shows a deduction as a positive number inside a deduction section, extraction flips the sign and adds audit flag `sign_normalized`.

```yaml
buckets:
  GROSS_POTENTIAL_RENT:
    section: revenue
    sign: positive
    description: Rent for all units at market or scheduled rent, before any deductions.
    synonyms: ["Gross Potential Rent", "GPR", "Potential Rent", "Gross Market Rent", "Scheduled Rent", "Market Rent"]
  LOSS_TO_LEASE:
    section: revenue
    sign: negative
    description: Gap between market rent and in-place lease rent on occupied units.
    synonyms: ["Loss to Lease", "Loss/Gain to Lease", "Gain/Loss to Lease", "LTL"]
  VACANCY_LOSS:
    section: revenue
    sign: negative
    description: Rent lost on vacant and down units.
    synonyms: ["Vacancy", "Vacancy Loss", "Physical Vacancy", "Vacancy & Credit Loss"]
  CONCESSIONS:
    section: revenue
    sign: negative
    description: Free rent and rent discounts given to tenants.
    synonyms: ["Concessions", "Rent Concessions", "Free Rent", "Move-in Specials"]
  BAD_DEBT:
    section: revenue
    sign: negative
    description: Rent billed but not collected and written off.
    synonyms: ["Bad Debt", "Bad Debt Expense", "Write-offs", "Collection Loss"]
  UTILITY_REIMBURSEMENT:
    section: revenue
    sign: positive
    description: Utility charges billed back to tenants (RUBS, submetering).
    synonyms: ["RUBS", "Utility Reimbursement", "Water/Sewer Reimbursement", "Utility Billback"]
  OTHER_INCOME:
    section: revenue
    sign: positive
    description: Recurring fees and ancillary income such as parking, pet, laundry, late, application fees.
    synonyms: ["Other Income", "Misc Income", "Parking Income", "Pet Fees", "Laundry Income", "Late Fees", "Application Fees"]
  REAL_ESTATE_TAXES:
    section: opex
    sign: positive
    description: Property taxes paid to the taxing authority.
    synonyms: ["Real Estate Taxes", "R/E Taxes", "Property Taxes", "RE Tax"]
  INSURANCE:
    section: opex
    sign: positive
    description: Property and liability insurance premiums.
    synonyms: ["Insurance", "Property Insurance", "Liability Insurance"]
  UTILITIES:
    section: opex
    sign: positive
    description: Owner-paid electric, gas, water, sewer, trash.
    synonyms: ["Utilities", "Electric", "Gas", "Water/Sewer", "Water & Sewer", "Trash Removal"]
  REPAIRS_MAINTENANCE:
    section: opex
    sign: positive
    description: Ordinary repairs and maintenance supplies, not capital items.
    synonyms: ["Repairs & Maintenance", "R&M", "Repairs and Maintenance", "Maintenance Supplies"]
  TURNOVER:
    section: opex
    sign: positive
    description: Make-ready costs between tenants (paint, cleaning, carpet).
    synonyms: ["Turnover", "Make Ready", "Make-Ready", "Unit Turns", "Turn Costs"]
  CONTRACT_SERVICES:
    section: opex
    sign: positive
    description: Recurring outside contracts such as landscaping, pest control, snow removal, elevator.
    synonyms: ["Contract Services", "Landscaping", "Pest Control", "Snow Removal", "Elevator Contract"]
  PAYROLL:
    section: opex
    sign: positive
    description: On-site staff wages, payroll taxes and benefits.
    synonyms: ["Payroll", "Salaries", "Wages", "Payroll & Benefits", "Personnel"]
  MANAGEMENT_FEE:
    section: opex
    sign: positive
    description: Property management fee, usually a percent of collections.
    synonyms: ["Management Fee", "Management Fees", "Property Management"]
  GENERAL_ADMIN:
    section: opex
    sign: positive
    description: Office, software, legal, accounting, licenses, bank fees.
    synonyms: ["General & Administrative", "G&A", "Administrative", "Office Expense", "Legal & Accounting"]
  MARKETING:
    section: opex
    sign: positive
    description: Advertising and leasing promotion.
    synonyms: ["Marketing", "Advertising", "Marketing & Advertising"]
  CAPEX_RESERVES:
    section: below_noi
    sign: positive
    description: Capital expenditures and replacement reserves. Below NOI.
    synonyms: ["Capital Expenditures", "CapEx", "Replacement Reserves", "Reserves"]
  NON_OPERATING:
    section: excluded
    sign: positive
    description: Debt service, interest, depreciation, amortization, owner or partnership costs, income taxes.
    synonyms: ["Debt Service", "Mortgage Interest", "Interest Expense", "Depreciation", "Amortization", "Partnership Expenses", "Asset Management Fee"]
  UNMAPPED:
    section: control
    sign: positive
    description: Not yet classified. Always blocks export until a human maps it.
    synonyms: []

nonrecurring_keywords:
  - insurance proceeds
  - insurance claim
  - lease buyout
  - lease termination fee
  - settlement
  - refund
  - one-time
  - one time
  - non-recurring
  - nonrecurring
  - legal recovery

subtotal_labels:
  total_income: ["Total Income", "Effective Gross Income", "EGI", "Total Revenue", "Total Operating Income"]
  total_expenses: ["Total Expenses", "Total Operating Expenses", "Total OpEx"]
  noi: ["Net Operating Income", "NOI"]
```

### 5.2 `config/rr_headers.yaml`

```yaml
fuzzy_min_score: 90          # rapidfuzz token_set_ratio on normalized text
required_fields: [unit_id, in_place_rent]
status_or_tenant_required: true   # need a status column OR a tenant_name column

fields:
  unit_id: ["Unit", "Unit #", "Unit No", "Unit Number", "Apt", "Apt #", "Suite"]
  unit_type: ["Unit Type", "Type", "Floor Plan", "Floorplan", "Plan"]
  beds: ["Beds", "BR", "Bedrooms", "Bd"]
  baths: ["Baths", "BA", "Bathrooms", "Ba"]
  sq_ft: ["SqFt", "Sq Ft", "SF", "Square Feet", "Area", "Unit SF"]
  status: ["Status", "Unit Status", "Occupancy", "Occ Status"]
  tenant_name: ["Tenant", "Resident", "Tenant Name", "Resident Name", "Lessee"]
  in_place_rent: ["Rent", "Lease Rent", "Contract Rent", "Current Rent", "In-Place Rent", "Actual Rent", "Charged Rent"]
  market_rent: ["Market Rent", "Market", "Mkt Rent", "Mkt", "Asking Rent", "Scheduled Rent"]
  lease_start: ["Lease Start", "Lease From", "Move In", "Move-In", "Start Date", "Lease Begin"]
  lease_end: ["Lease End", "Lease To", "Lease Expiration", "Expiration", "End Date", "Lease Exp"]
  concession_note: ["Concession", "Concessions", "Notes", "Comments", "Specials", "Remarks"]

status_synonyms:
  occupied: ["occupied", "occ", "o", "current", "leased", "occupied-nr", "active"]
  vacant: ["vacant", "vac", "v", "vacant-unrented", "vacant unrented"]
  notice: ["notice", "ntv", "on notice", "occupied-ntv", "notice to vacate"]
  model: ["model", "model unit"]
  down: ["down", "offline", "non-rentable", "down unit"]
  employee: ["employee", "staff", "courtesy officer"]

vacant_tenant_markers: ["VACANT", "VACANT UNIT", "*VACANT*"]
```

### 5.3 `config/thresholds.yaml`

Every default here is a **starting placeholder** to be calibrated on real deals. The README must say so.

```yaml
rr_footing:
  tolerance_usd: "1.00"              # strings so they load as Decimal
  market_rent_severity: warn         # footing failure on market rent column
  max_reextract_attempts: 1
rr_zero_rent_occ:
  occupied_like_statuses: [occupied, notice]
rr_expired_lease:
  occupied_like_statuses: [occupied, notice]
  treat_missing_lease_end_as_mtm: true
rr_concession:
  drag_warn_pct: "0.02"              # monthly concession equiv / total in-place rent
  default_lease_term_months: 12
rr_rent_outlier:
  deviation_pct: "0.25"
  min_group_size: 3
x_rr_vs_t3:
  gap_warn_pct: "0.05"
  occupied_like_statuses: [occupied, notice]
  t3_rental_buckets: [GROSS_POTENTIAL_RENT, LOSS_TO_LEASE, VACANCY_LOSS]
t12_footing:
  line_tolerance_usd: "1.00"
  subtotal_tolerance_usd: "5.00"
t12_spike:
  factor: "3.0"                      # month > factor x median of the line's 12 months
  min_abs_usd: "1000.00"
  exempt_buckets: [REAL_ESTATE_TAXES, INSURANCE]   # often paid in lumps
  checked_sections: [opex, revenue]
pf_tax_drop:
  drop_pct: "0.00"                   # any decrease vs T-12 warns
pf_ins_drop:
  drop_pct: "0.10"
pf_vacancy:
  margin: "0.005"
  vacant_like_statuses: [vacant, down]
pf_mgmt_fee:
  margin: "0.005"
review:
  override_note_min_chars: 10
```

### 5.4 `config/models.yaml`

Prices from Anthropic's pricing page (platform.claude.com/docs/en/about-claude/pricing), checked 2026-11-06. Re-check before the final run.

```yaml
provider: anthropic
default_model: claude-haiku-5-5
comparison_model: claude-sonnet-5-5
temperature: 0
max_retries: 2

max_tokens:
  header_map: 1024
  coa_map: 4096
  concession: 1024
  vision_page: 4096

max_prompt_tokens: 90000      # Haiku 5.5 input price rises 5x above 100,000 prompt tokens
chars_per_token_estimate: 3   # conservative pre-call size guard

pricing_usd_per_mtok:
  claude-haiku-5-5:
    input: "0.10"
    output: "0.50"
    long_context_threshold: 100000
    input_long: "0.50"
    output_long: "2.50"
  claude-sonnet-5-5:
    input: "2.00"
    output: "10.00"

budget:
  total_cap_usd: "10.00"      # lifetime, enforced against .cost_ledger.jsonl
  per_run_cap_usd: "1.00"     # override with --run-cap for the Sonnet comparison

cache:
  dir: evals/llm_cache
  mode: replay                # replay | record | off
  version: 1                  # bump to invalidate every cache entry
```

---

## 6. Data contracts (Pydantic v2)

All models use `ConfigDict(extra="forbid")` unless stated. Files below are complete.

### 6.1 `schemas/common.py`

```python
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
```

A unit test asserts `len(REVENUE_BUCKETS) == 7`, `len(OPEX_BUCKETS) == 10`, `len(CONTROL_BUCKETS) == 3`, and that `config/coa.yaml` has exactly the 20 bucket keys in this order.

### 6.2 `schemas/citation.py`

```python
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
```

### 6.3 `schemas/rent_roll.py`

```python
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .citation import Citation
from .common import Money, RRField, SourceFormat, Status


class Concession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["free_months", "monthly_discount", "one_time_credit", "other"]
    monthly_equivalent: Money = Field(ge=Decimal("0"))  # computed by code, never by the LLM
    months_free: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("24"))
    monthly_discount: Money | None = Field(default=None, ge=Decimal("0"))
    one_time_credit: Money | None = Field(default=None, ge=Decimal("0"))
    lease_term_months: int | None = Field(default=None, ge=1, le=60)
    raw_text: str
    grounded: bool  # every LLM number found in raw_text
    citation: Citation


class RentRollUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str = Field(min_length=1)
    unit_type: str | None = None
    beds: int | None = Field(default=None, ge=0, le=6)
    baths: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("6"))
    sq_ft: int | None = Field(default=None, gt=0, lt=10_000)
    status: Status
    in_place_rent: Money | None = Field(default=None, ge=Decimal("0"))  # $0 allowed on purpose
    market_rent: Money | None = Field(default=None, ge=Decimal("0"))
    lease_start: date | None = None
    lease_end: date | None = None
    concessions: list[Concession] = []
    audit_flags: list[str] = []
    citations: dict[str, Citation]  # field name -> source; "unit_id" required

    @model_validator(mode="after")
    def _checks(self) -> RentRollUnit:
        if "unit_id" not in self.citations:
            raise ValueError(f"{self.unit_id}: missing unit_id citation")
        if self.lease_start and self.lease_end and self.lease_end < self.lease_start:
            raise ValueError(f"{self.unit_id}: lease_end before lease_start")
        return self


class SubtotalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    column: Literal["in_place_rent", "market_rent"]
    scope: Literal["page", "unit_type", "grand_total"]
    stated_value: Money
    covers_unit_ids: list[str]  # units assigned to this block
    citation: Citation


class ColumnMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    header_text: str
    maps_to: RRField


class HeaderMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[ColumnMap]
    source: Literal["synonym_table", "llm", "manual_override"]

    @model_validator(mode="after")
    def _one_to_one(self) -> HeaderMap:
        used = [c.maps_to for c in self.columns if c.maps_to != "ignore"]
        dupes = sorted({f for f in used if used.count(f) > 1})
        missing = sorted({"unit_id", "in_place_rent"} - set(used))
        no_status = "status" not in used and "tenant_name" not in used
        problems = []
        if dupes:
            problems.append(f"mapped more than once: {dupes}")
        if missing:
            problems.append(f"missing required: {missing}")
        if no_status:
            problems.append("need a 'status' or 'tenant_name' column")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class RentRoll(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_name: str | None = None
    as_of_date: date | None = None
    source_file: str
    source_format: SourceFormat
    header_map: HeaderMap
    units: list[RentRollUnit]
    stated_totals: dict[Literal["in_place_rent", "market_rent"], Money] = {}
    stated_total_citations: dict[Literal["in_place_rent", "market_rent"], Citation] = {}
    subtotals: list[SubtotalRow] = []
    extraction_warnings: list[str] = []

    @model_validator(mode="after")
    def _totals_cited(self) -> RentRoll:
        if set(self.stated_totals) != set(self.stated_total_citations):
            raise ValueError("every stated total needs a citation")
        return self
```

`as_of_date` is required by the expired-lease check. If the document has none, extraction sets `None` and adds warning `"no as-of date found"`; the CLI then requires `--as-of YYYY-MM-DD`.

### 6.4 `schemas/t12.py`

```python
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .citation import Citation
from .common import COABucket, Money, SourceFormat


class T12Line(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    bucket: COABucket
    monthly: list[Money] = Field(min_length=12, max_length=12)
    stated_total: Money | None = None
    is_nonrecurring: bool = False
    mapping_source: Literal["synonym_table", "fuzzy", "llm", "manual_override"]
    mapping_confidence: float = Field(ge=0.0, le=1.0)  # derived from route, never LLM-reported
    audit_flags: list[str] = []
    citation: Citation  # the row
    cell_citations: dict[str, Citation] = {}  # "m01".."m12", "total"

    @property
    def include_in_uw(self) -> bool:
        # Plain property, not @computed_field: a computed field is serialized and then
        # rejected by extra="forbid" on the way back in, breaking JSON round-trips.
        return not self.is_nonrecurring and self.bucket not in ("NON_OPERATING", "UNMAPPED")

    def computed_total(self) -> Decimal:
        return sum(self.monthly, Decimal("0"))

    def t3_annualized(self) -> Decimal:
        return sum(self.monthly[9:12], Decimal("0")) * 4


class StatedSubtotal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    kind: Literal["total_income", "total_expenses", "noi", "other"]
    monthly: list[Money | None] = Field(min_length=12, max_length=12)
    total: Money | None = None
    citation: Citation


class T12Statement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_name: str | None = None
    period_end: date  # last day of month 12
    month_labels: list[str] = Field(min_length=12, max_length=12)
    source_file: str
    source_format: SourceFormat
    lines: list[T12Line]
    stated_subtotals: list[StatedSubtotal] = []
    extraction_warnings: list[str] = []

    @model_validator(mode="after")
    def _unique_labels(self) -> T12Statement:
        labels = [ln.label for ln in self.lines]
        if len(labels) != len(set(labels)):
            raise ValueError("duplicate T-12 line labels; extractor must suffix duplicates ' (2)'")
        return self

    def bucket_total(self, bucket: str, *, uw_only: bool = False) -> Decimal:
        return sum(
            (
                ln.computed_total()
                for ln in self.lines
                if ln.bucket == bucket and (ln.include_in_uw or not uw_only)
            ),
            Decimal("0"),
        )

    def bucket_t3_annualized(self, bucket: str, *, uw_only: bool = False) -> Decimal:
        return sum(
            (
                ln.t3_annualized()
                for ln in self.lines
                if ln.bucket == bucket and (ln.include_in_uw or not uw_only)
            ),
            Decimal("0"),
        )
```

### 6.5 `schemas/proforma.py`

```python
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .citation import Citation
from .common import COABucket, Money, Rate


class BrokerProForma(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_name: str
    asking_price: Money | None = Field(default=None, gt=Decimal("0"))  # None if unpriced
    unit_count: int | None = Field(default=None, ge=1)
    lines: dict[COABucket, Money]  # annual, coa sign conventions
    vacancy_rate: Rate | None = None  # if the broker states a %
    mgmt_fee_pct: Rate | None = None  # if the broker states a %
    source_note: str  # e.g. "OM p.14 pro forma table"
    citation: Citation  # provenance="manual_entry"

    @model_validator(mode="after")
    def _no_control_buckets(self) -> BrokerProForma:
        bad = {"NON_OPERATING", "UNMAPPED"} & set(self.lines)
        if bad:
            raise ValueError(f"pro forma cannot use control buckets: {sorted(bad)}")
        return self
```

`proforma.yaml` format:

```yaml
property_name: "Erdrick Court Apartments"
asking_price: "7500000.00"        # or null
unit_count: 48
vacancy_rate: "0.03"              # or null
mgmt_fee_pct: null
source_note: "OM p.14, 'Pro Forma Year 1' table"
lines:
  GROSS_POTENTIAL_RENT: "1020000.00"
  VACANCY_LOSS: "-30600.00"
  OTHER_INCOME: "24000.00"
  REAL_ESTATE_TAXES: "98000.00"
  INSURANCE: "36000.00"
  UTILITIES: "54000.00"
  REPAIRS_MAINTENANCE: "38000.00"
  PAYROLL: "72000.00"
  MANAGEMENT_FEE: "30000.00"
  GENERAL_ADMIN: "12000.00"
  MARKETING: "6000.00"
  CAPEX_RESERVES: "12000.00"
```

Money values are quoted strings so YAML never turns them into floats.

### 6.6 `schemas/mapping.py` (LLM-facing response models)

These are the only models passed to `response_model=`. None of them carries a dollar amount except `ConcessionParse` (grounded) and `VisionRow` (strings only).

```python
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import COABucket, RRField


class LLMColumnMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    header_text: str
    maps_to: RRField


class LLMHeaderMap(BaseModel):
    """Converted to rent_roll.HeaderMap(source='llm'); its validator enforces one-to-one."""

    model_config = ConfigDict(extra="forbid")
    columns: list[LLMColumnMap]


class COAMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_label: str
    bucket: COABucket
    is_nonrecurring: bool
    rationale: str = Field(max_length=160)


class COAMappingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: list[COAMapping]


class ConcessionParse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["free_months", "monthly_discount", "one_time_credit", "other"]
    months_free: Decimal | None = None
    monthly_discount: Decimal | None = None
    one_time_credit: Decimal | None = None
    lease_term_months: int | None = None


class VisionRow(BaseModel):
    """Strings exactly as printed. Code parses them. Empty string if the cell is blank."""

    model_config = ConfigDict(extra="forbid")
    cells: dict[str, str]  # header_text -> printed text
    row_kind: Literal["unit", "subtotal", "header", "footnote", "other"]


class VisionTablePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headers: list[str]
    rows: list[VisionRow]
```

### 6.7 `schemas/findings.py`

```python
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
```

### 6.8 `schemas/deal.py`

```python
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import COABucket, Money, Status
from .findings import ExpectedFinding, Finding, GateState, GateTransition, Override
from .proforma import BrokerProForma
from .rent_roll import RentRoll
from .t12 import T12Statement


class DealPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    run_id: str  # "<UTC yyyymmddTHHMMSS>-<8 hex>"
    model_id: str
    rent_roll: RentRoll
    t12: T12Statement | None
    proforma: BrokerProForma | None
    findings: list[Finding]
    gate_log: list[GateTransition]
    final_state: GateState
    overrides: list[Override] = []
    cost_usd: Decimal = Decimal("0")
    wall_seconds: float = 0.0
    created_at: datetime

    def open_blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block" and f.status == "open"]


# ---------- synthetic ground truth ----------


class TruthUnit(BaseModel):
    """Values exactly as the DOCUMENT shows them (traps included)."""

    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_type: str | None
    beds: int | None
    baths: Decimal | None
    sq_ft: int | None
    status: Status
    in_place_rent: Money | None
    market_rent: Money | None
    lease_start: date | None
    lease_end: date | None
    concession_note: str | None = None
    page: int | None = None  # PDF page where the row is printed


class TruthT12Line(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    true_bucket: COABucket
    monthly: list[Money] = Field(min_length=12, max_length=12)
    stated_total: Money
    is_nonrecurring: bool


class DealTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    seed: int
    split: Literal["dev", "heldout"]
    rr_layout: str
    t12_layout: str
    rr_format: Literal["pdf_text", "pdf_scanned", "xlsx"]
    t12_format: Literal["pdf_text", "xlsx"]
    as_of_date: date
    units: list[TruthUnit]
    rr_stated_totals: dict[str, Money]
    t12_lines: list[TruthT12Line]
    traps: list[str]
    expected: list[ExpectedFinding]


class DealScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deal_id: str
    split: str
    provenance_mix: dict[str, int]  # provenance -> numeric fields scored
    numeric_fields_total: int
    numeric_fields_correct: int
    units_in_doc: int
    units_found: int
    phantom_units: int
    coa_dollars_total: Decimal
    coa_dollars_correct: Decimal
    planted: int
    caught: int
    false_positives: int  # warn/block findings matching no trap
    clean_control_findings: int  # warn/block findings on a clean deal
    cost_usd: Decimal
    wall_seconds: float
    misses: list[str]  # "trap_id: reason"
```

---

## 7. Module specs

### 7.1 Ingestion (`ingest/`)

**`raw.py`**

```python
@dataclass(frozen=True)
class RawCell:
    text: str
    bbox: tuple[float, float, float, float] | None  # PDF only
    cell: str | None  # Excel only, e.g. "D14"


@dataclass
class RawRow:
    page: int | None
    sheet: str | None
    row_index: int
    kind: Literal["unit", "subtotal", "header", "continuation", "footnote", "blank", "line"]
    cells: dict[str, RawCell]  # keyed by header_text


@dataclass
class RawTable:
    source_file: str
    source_format: SourceFormat
    headers: list[str]
    rows: list[RawRow]
    page_count: int
    header_texts_by_page: dict[int, list[str]]
    preamble_text: str  # text above the first header (as-of date, property name)
```

**`router.py`**

- `detect_format(path: Path) -> SourceFormat`: `.xlsx` returns `"xlsx"`. `.pdf`: open with pdfplumber; if more than half of pages have fewer than 20 extractable characters, return `"pdf_scanned"`, else `"pdf_text"`. Any other extension raises `UnsupportedFileError`.

**`parse.py`** (pure functions, fully property-tested)

- `parse_money(text: str) -> Decimal | None`:
  - Strip whitespace, `$`, `,`, non-breaking spaces.
  - `""`, `"N/A"`, `"n/a"` return `None`.
  - `"-"`, `"–"`, `"—"` alone return `Decimal("0.00")` (accounting zero).
  - `"(123.45)"` and `"123.45-"` and `"−123.45"` (Unicode minus) return negative.
  - More than 2 decimal places: quantize with `to_cents`; caller adds audit flag `rounded_to_cents`.
  - Anything else raises `MoneyParseError(text)`.
- `parse_date(text: str, *, end_of_month: bool = False) -> date | None`: formats tried in order: `%m/%d/%Y`, `%m/%d/%y`, `%Y-%m-%d`, `%d-%b-%Y`, `%b-%y`, `%b %Y`, `%m/%Y`. Month-only formats return the first day, or the last day when `end_of_month=True` (used for lease end). Excel `datetime` values pass through as `date`. Blank returns `None`. Unparseable raises `DateParseError`.
- `parse_int`, `parse_decimal`: same blank and error rules.
- `normalize_status(text: str, tenant: str | None, rent: Decimal | None) -> Status`: lowercase match against `status_synonyms`. If no status column: tenant in `vacant_tenant_markers` or blank tenant with blank rent gives `vacant`, otherwise `occupied`. Unknown text gives `unknown` plus audit flag.

**`pdf_tables.py`** (rent roll and T-12 share it)

1. For each page: `page.extract_words(keep_blank_chars=False, use_text_flow=False)`.
2. Header detection: on page 1, find the line (words grouped by `top` within 3.0 pt) whose words best match `rr_headers.yaml` (or T-12 month labels for T-12). Score = number of fields matched at `fuzzy_min_score`. Header line must match at least 2 fields. Multi-word headers are merged by x-adjacency (gap under 6 pt).
3. Column boundaries: midpoints between adjacent header x-centers; first column starts at 0, last ends at page width.
4. Rows: cluster remaining words by `top` with tolerance 3.0 pt. Assign each word to the column with the greatest horizontal overlap.
5. Row kind: header text repeated on later pages is `header`; a row whose first non-empty cell matches `/total|subtotal/i` is `subtotal`; rows with only one cell starting with `*` or a digit followed by `)` are `footnote`; rows with a parseable unit-id-like first cell are `unit`; T-12 rows with a label and at least one numeric cell are `line`.
6. Release each page's objects before opening the next (`page.close()` where available).
7. The function is pure with respect to its input file; results are cached as JSON at `out/cache/extract/<sha256 of file>.json`.

**`excel_tables.py`**: open with `openpyxl.load_workbook(path, data_only=True, read_only=True)`. Use the first sheet unless `--sheet` is given. Find the header row with the same scoring in the first 30 rows. Row kinds as above. `RawCell.cell` holds the address.

**`render.py`**: the only module that imports `fitz`. Functions: `page_count(pdf) -> int`, `render_page_png(pdf: Path, page: int, dpi: int = 110) -> bytes`, `draw_boxes(png: bytes, boxes: list[tuple[float, float, float, float]], scale: float) -> bytes`, `rasterize_pdf(pdf: Path, out: Path, dpi: int) -> None` (used by `synth/degrade.py`). A docstring states the AGPL license and that `pypdfium2` can replace it behind the same four functions.

### 7.2 Header mapping (`mapping/header_mapper.py`)

1. Normalize each header (lowercase, strip punctuation, collapse spaces). Match against synonyms with `rapidfuzz.fuzz.token_set_ratio`. Each field may be used once (highest score wins).
2. If the result passes `HeaderMap` validation, return it with `source="synonym_table"`. No LLM call.
3. Otherwise call the LLM (`task="header_map"`) with the header strings plus the raw text of the first 3 unit rows. Response model `LLMHeaderMap`.
4. Code check: every input header appears exactly once in the response (`header_text` exact match). Missing headers are added as `ignore`; extra headers are dropped and a warning added.
5. Convert to `HeaderMap(source="llm")`. If validation fails after the retry, raise `HeaderMapError`; the pipeline marks the deal `REVIEW` with a block-severity extraction warning and stops.

### 7.3 Rent roll building (`extract/rent_roll.py`)

- Input: `RawTable`, `HeaderMap`, config. Output: `RentRoll`.
- One `RentRollUnit` per `unit` row. Each populated field gets a `Citation` (page, row_index, the cell's bbox, raw text, provenance).
- `subtotal` rows become `SubtotalRow`s. Scope: grand total if the label matches `/grand total|total/i` and it is the last subtotal in the document, `unit_type` if units since the previous subtotal share one unit type, otherwise `page`. `covers_unit_ids` = units between the previous subtotal (or page start) and this row. The grand total row also populates `stated_totals`.
- Concession notes go to `mapping/concessions.py` (7.5).
- Property name and as-of date come from `preamble_text` with regex `as of[:\s]+(.+)` and the first non-empty line, respectively.

### 7.4 COA mapping (`mapping/coa_mapper.py`)

Order per line label:

1. Exact synonym match (case-insensitive, punctuation-stripped) gives `mapping_source="synonym_table"`, confidence 1.0.
2. Fuzzy synonym match with score of at least 92 gives `"fuzzy"`, confidence = score / 100.
3. Remaining labels go to the LLM in **one batch per statement** (`task="coa_map"`), with bucket names and descriptions from `coa.yaml`. Response model `COAMappingBatch`.
4. Code check: every sent label returns exactly once. Missing labels are re-sent once in a second call; still missing gives `UNMAPPED`.
5. LLM-mapped lines get `mapping_source="llm"`, confidence 0.7.
6. `is_nonrecurring` is `True` if any `nonrecurring_keywords` entry appears in the label (case-insensitive) OR the LLM said so.
7. Approved mappings from the UI (`remap_line` overrides) are appended to `config/coa_learned.yaml` (same shape as `synonyms`, created on first write) and read in step 1 on later runs.

Rows with `kind == "subtotal"` matching `subtotal_labels` become `StatedSubtotal`s instead of lines.

### 7.5 Concession parsing (`mapping/concessions.py`)

1. Regex fast path: `(\d+(?:\.\d+)?)\s*(?:mo|month)s?\s*free` with optional `on\s*(\d+)\s*-?\s*(?:mo|month)`; `\$?(\d[\d,]*(?:\.\d+)?)\s*(?:off|discount)\s*(?:/|per)?\s*mo`. A match skips the LLM.
2. Otherwise LLM (`task="concession"`) with response model `ConcessionParse`.
3. **Grounding check:** for each non-null number the LLM returned, its plain string form (no trailing zeros, e.g. `1`, `13`, `150`) must appear in the note after removing `$` and `,`. Fail sets `grounded=False`, `monthly_equivalent=0`, audit flag `concession_ungrounded`, and the unit is listed in the `RR_CONCESSION` finding's details for review.
4. `monthly_equivalent` (code): `free_months`: `in_place_rent × months_free / lease_term_months` (term default from thresholds). `monthly_discount`: the discount. `one_time_credit`: `credit / lease_term_months`. Quantize with `to_cents`.

### 7.6 Vision fallback (`mapping/vision.py`)

- Only for `pdf_scanned` rent rolls.
- Render each page at 110 dpi with `render.render_page_png`. Send as an Anthropic image content block (base64 PNG) with `task="vision_page"`, response model `VisionTablePage`.
- Convert to `RawTable` (no bbox; citation page and row_index only) and continue through 7.2 and 7.3 unchanged. Every citation gets `provenance="llm_vision"`.
- Each page is a separate call so a prompt stays far below `max_prompt_tokens`.
- Title lines come back as `other` rows before the first header row on page 1; they become `RawTable.preamble_text`, which is where the as-of date is read.

### 7.7 LLM client, cache, cost (`mapping/llm_client.py`, `cache.py`, `cost.py`)

```python
TaskName = Literal["header_map", "coa_map", "concession", "vision_page"]


class LLMClient(Protocol):
    def structured(
        self,
        *,
        task: TaskName,
        response_model: type[T],
        system: str,
        user_content: list[dict],  # Anthropic content blocks (text and image)
        model: str | None = None,
    ) -> T: ...


class AnthropicLLMClient:  # real implementation
    def __init__(self, cfg: ModelsConfig, cache: LLMCache, tracker: CostTracker) -> None: ...
```

Call sequence inside `AnthropicLLMClient.structured`:

1. Build the cache key: `sha256(json.dumps({"v": cache.version, "model": model, "task": task, "system": system, "user_content": user_content, "schema": response_model.model_json_schema(), "temperature": cfg.temperature, "max_tokens": cfg.max_tokens[task]}, sort_keys=True, ensure_ascii=True))`.
2. Cache hit: validate the stored JSON into `response_model` and return. Cost 0.
3. Cache miss in `replay` mode: raise `CacheMissError(task, key)`.
4. Size guard: estimated prompt tokens = total characters / `chars_per_token_estimate` (images count 1,600 each). Above `max_prompt_tokens`: raise `PromptTooLargeError`.
5. Budget guard: `tracker.check_can_spend(worst_case_cost(model, est_input, max_tokens))`. Raises `BudgetExceededError` if ledger lifetime total + this run + worst case passes `total_cap_usd`, or this run + worst case passes the run cap.
6. Call Instructor (`instructor.from_anthropic(anthropic.Anthropic())`, `messages.create(..., response_model=..., max_retries=cfg.max_retries, temperature=0)`). Register an Instructor hook on each completion response so **every attempt's** `usage` is recorded, not only the last. Confirm hook names against the installed Instructor version; if hooks are unavailable, fall back to recording the final attempt's usage multiplied by attempts and note it in the ledger line.
7. Record each attempt in `.cost_ledger.jsonl`: `{"ts", "run_id", "task", "model", "input_tokens", "output_tokens", "cost_usd", "attempt"}`.
8. In `record` mode, write `evals/llm_cache/<key[:2]>/<key>.json`: `{"key", "task", "model", "created_at", "response", "usage": {"input_tokens", "output_tokens", "attempts"}, "cost_usd"}`. Writes are atomic (temp file then `Path.replace`).

Cost formula (Decimal): `input_tokens × input_price / 1,000,000 + output_tokens × output_price / 1,000,000`, using the long-context prices when the model defines `long_context_threshold` and `input_tokens` exceeds it.

`tests/fakes.py::FakeLLMClient` implements the same protocol from a dict `{(task, frozenset_of_key_inputs): response_object}` and records calls, so tests can assert the LLM was **not** called on the synonym-table path.

### 7.8 Pipeline (`pipeline.py`)

```python
def run_deal(
    deal_dir: Path,
    *,
    llm: LLMClient,
    cfg: AppConfig,
    as_of: date | None = None,
    overrides: list[Override] | None = None,
) -> DealPackage: ...
```

Steps: detect formats, extract raw tables, map headers, build rent roll, extract and map T-12, load pro forma, apply overrides, run the gate (Section 8), return `DealPackage`. Writes `out/<deal_id>/package.json`. Never writes inside `data/`.

### 7.9 Reading real-world files (`read_inputs.py`, `ingest/routing.py`, `ingest/unwrap.py`)

- **Routing.** For a text-layer PDF, `route_pdf` decides which pages (or which horizontal slice of a shared page) hold the unit-level rent roll and the T-12 grid. It routes when one page holds both tables, when both tables are in one file, or when a file of more than 4 pages has pages that are plainly something else (pro forma, photos, highlights). A plain single-table file is read whole, exactly as before. Pages whose title starts with a pro forma heading are never taken for the rent roll. The same file may be supplied in both slots; a rent roll file that also holds the T-12 serves as the T-12 when no `t12` file exists.
- **Reader order.** Rent roll: (1) text layer, synonym-table headers only; (2) text layer with wrapped cells rejoined; (3) text layer with the model reading the headers (7.2); (4) rejoined text layer with the model reading the headers; (5) vision on the routed pages (7.6). T-12: text layer, rejoined text layer, vision with `vision_t12.md`. A step that raises a parse, header-map, T-12 or vision error, or yields no unit rows (no 12 month columns or no line items for a T-12), hands over to the next. Each failed step adds a note to `extraction_warnings`. Vision is allowed only on routed pages or on a file of at most 4 pages, so a brochure is never sent page by page.
- **Wrapped cells.** `unwrap_words` merges stacks of fragments that sit closer than one text height: `Uni`/`t` becomes `Unit`, `04/03/2`/`6` becomes `04/03/26`, `$110,95`/`6` becomes `$110,956`. Fragments join without a space inside a number, date or split word and with a space otherwise. The merged word keeps a box covering every fragment.
- **Headerless label column.** If the first printed T-12 header is a month and the lines below carry text to its left, that column is the label column and gets the header `Line Item`. Month names without a year (`Jan` .. `Dec`) take the year from the title text above the table; with no year found the T-12 is rejected.
- **Notes under a T-12.** A row is a line item only if at least as many of its cells are figures as are words.
- **Cache.** The extraction cache key includes `READER_VERSION`; bump it whenever a table reader changes what it produces.
- **Pre-flight (UI).** Before Run, `ingest/preflight.py` checks the files and shows plain-English banners. A file is blocked only when it is damaged, blank, mirrored, or when a required table is absent: a rent roll slot with no unit-level rows anywhere (a P&L, a unit-mix summary), or a T-12 slot with no income and expense table. Routed pages are announced as information. The same file in both slots is not an error.

---

## 8. Reconciliation gate (`reconcile/`)

### 8.1 State machine (`gate.py`)

States: `FOOTING`, `REEXTRACT`, `CHECKS`, `REVIEW`, `READY`. Start state `FOOTING`. Every transition appends a `GateTransition`.

```text
FOOTING:
    f = rr_footing(ctx)                     # grand total and every SubtotalRow, in_place_rent
    if no failing in_place_rent footing:    -> CHECKS
    elif attempts < max_reextract_attempts: attempts += 1; -> REEXTRACT
    else:                                   record f (block) ; -> CHECKS
REEXTRACT:
    block = localize(ctx)                   # smallest failing SubtotalRow scope; else whole table
    reread = reread_block(ctx, block)       # reconcile/reread.py
    if reread agrees with original on every unit in block:
        f.details["cause"] = "document"; record f; -> CHECKS
    else:
        f.details["cause"] = "extraction"; f.unit_ids = disagreeing units; record f; -> CHECKS
CHECKS:
    run every check in REGISTRY order except RR_FOOTING; append findings
    -> REVIEW if any open block finding else READY
```

`REEXTRACT` never loops back to `FOOTING` automatically; a second footing pass happens only after a human edits values in the UI (which re-runs the whole gate from `FOOTING`).

**`reread.py`**: text-layer PDF: re-cluster the block's pages with row tolerance 1.5 pt and column boundaries taken from the x-extents of the data cells (not the header). Excel: re-read the block's cells by address with `data_only=True` from a fresh workbook load. Scanned: one more vision call on the block's page with `task="vision_page"` and a system prompt variant asking for a careful re-transcription (separate cache key). "Agree" means every money field of every unit in the block is equal to the cent.

### 8.2 Check interface

```python
@dataclass(frozen=True)
class CheckContext:
    rent_roll: RentRoll
    t12: T12Statement | None
    proforma: BrokerProForma | None
    thresholds: Thresholds
    as_of: date


Check = Callable[[CheckContext], list[Finding]]
REGISTRY: dict[CheckId, Check]  # in the order of the table below
```

Each check returns `[]` when its inputs are missing (no T-12, no pro forma). Each check is one file, one function, no I/O.

### 8.3 Check catalog (15 checks)

Definitions used below:

- occupied-like = status in the check's `occupied_like_statuses`.
- `rr_inplace_monthly` = sum of `in_place_rent` (None as 0) over occupied-like units.
- `physical_vacancy` = units with status in `vacant_like_statuses` / all units.
- `t12_egi_uw` = sum of `t12.bucket_total(b, uw_only=True)` over `REVENUE_BUCKETS`.

| # | Check ID | Logic | Severity | Thresholds |
| --- | --- | --- | --- | --- |
| 1 | `RR_FOOTING` | Grand total: compute sum of `in_place_rent` over all units and over occupied-like units; pass if either is within tolerance of `stated_totals["in_place_rent"]` (record which basis matched in `details["basis"]`). Each `SubtotalRow` for `in_place_rent`: sum over `covers_unit_ids` vs `stated_value`. Same for `market_rent` with `market_rent_severity`. No stated total: one `info` finding "footing not verifiable". | block (in-place), warn (market) | `rr_footing.tolerance_usd`, `market_rent_severity` |
| 2 | `RR_DUP_UNIT` | Normalize unit IDs (strip, uppercase, collapse internal spaces; keep leading zeros). Any ID appearing more than once: one finding listing all copies and their citations. | block | none |
| 3 | `RR_ZERO_RENT_OCC` | Occupied-like units with `in_place_rent` None or 0. One finding listing units. Units with status `model` or `employee` and $0 rent produce a separate `info` finding with the same check ID. | warn (info for model/employee) | `occupied_like_statuses` |
| 4 | `RR_EXPIRED_LEASE` | Occupied-like units with `lease_end < as_of` (or `lease_end` None if `treat_missing_lease_end_as_mtm`). One finding listing units; `details["mtm_share"]` = count / occupied-like count. | warn | `occupied_like_statuses`, `treat_missing_lease_end_as_mtm` |
| 5 | `RR_CONCESSION` | Units with any concession, or with an ungrounded concession note. `drag` = sum of `monthly_equivalent` / `rr_inplace_monthly`. Warn if `drag > drag_warn_pct` or any concession is ungrounded; else info. `details["drag"]`, `details["ungrounded_units"]`. | warn or info | `drag_warn_pct`, `default_lease_term_months` |
| 6 | `RR_RENT_OUTLIER` | Group units by `unit_type` (fallback `"{beds}x{baths}"`). Groups with at least `min_group_size` units having rent > 0. Median of positive rents. Units with rent > 0 and `|rent − median| / median > deviation_pct`. One finding per group. | warn | `deviation_pct`, `min_group_size` |
| 7 | `X_RR_VS_T3` | `rr_annual = 12 × rr_inplace_monthly`. `t3_annual = sum of t12.bucket_t3_annualized(b) for b in t3_rental_buckets`. Warn if `t3_annual > 0` and `|rr_annual − t3_annual| / t3_annual > gap_warn_pct`. Details: both values and gap. | warn | `gap_warn_pct`, `t3_rental_buckets` |
| 8 | `T12_FOOTING` | (a) Each line with `stated_total`: `|computed_total − stated_total| > line_tolerance_usd`. (b) Each `StatedSubtotal` total and each non-null month: `total_income` vs sum of revenue-bucket lines; `total_expenses` vs sum of opex-bucket lines; `noi` vs stated income − stated expenses when both exist, else vs computed. Difference > `subtotal_tolerance_usd`. One finding per failed line or subtotal. | block | `line_tolerance_usd`, `subtotal_tolerance_usd` |
| 9 | `T12_SPIKE` | Lines whose bucket section is in `checked_sections` and bucket not in `exempt_buckets`. Median of absolute monthly values; flag months where `value > factor × median` and `value − median > min_abs_usd`. Median 0 is skipped. One finding per line. | warn | `factor`, `min_abs_usd`, `exempt_buckets`, `checked_sections` |
| 10 | `T12_NONRECURRING` | Lines with `is_nonrecurring`. One finding listing labels and amounts; these lines are excluded from underwritten NOI. | warn | none |
| 11 | `T12_UNMAPPED` | Lines with bucket `UNMAPPED`. One finding listing labels. | block | none |
| 12 | `PF_TAX_DROP` | `pf = proforma.lines.get("REAL_ESTATE_TAXES")`, `t = t12.bucket_total("REAL_ESTATE_TAXES")`. Warn if both present and `pf < t × (1 − drop_pct)`. Message must say a sale may trigger reassessment and the drop needs support. | warn | `drop_pct` |
| 13 | `PF_INS_DROP` | Same pattern with `INSURANCE`. | warn | `drop_pct` |
| 14 | `PF_VACANCY` | `pf_vac = proforma.vacancy_rate` or else `−lines["VACANCY_LOSS"] / lines["GROSS_POTENTIAL_RENT"]`. Warn if `pf_vac < physical_vacancy − margin`. Skip if neither available. | warn | `margin`, `vacant_like_statuses` |
| 15 | `PF_MGMT_FEE` | `pf_pct = proforma.mgmt_fee_pct` or else `lines["MANAGEMENT_FEE"] / sum of pro forma revenue lines`. `t12_pct = t12.bucket_total("MANAGEMENT_FEE", uw_only=True) / t12_egi_uw`. Warn if `pf_pct < t12_pct − margin`. | warn | `margin` |

### 8.4 Review semantics

- Export is allowed when `DealPackage.open_blocks()` is empty.
- A block finding can be set to `overridden` only with a reviewer note of at least `override_note_min_chars` characters. An override is stored as an `Override` and re-applied on re-run.
- `edit_value` overrides change a unit's money field to `corrected_value`, set that field's citation provenance to `manual_override` (keeping the original raw text), and re-run the gate from `FOOTING`.
- `resolve_footing` (strategy `use_calculated_sum`) replaces a stated grand total with the computed sum of the unit rows. `corrected_value` must equal that sum to the cent. The stated total's citation becomes `manual_override` and keeps the printed text. `use_stated_total` and `edit_line_item` are `edit_value` overrides: the reviewer names the unit and the amount that makes the rows sum to the stated total. Nothing is plugged. If the rows still do not foot, the gate re-run leaves the block open.
- Data overrides are applied by `review.apply_data_overrides`, which is idempotent. The Excel writer calls it again on `pkg.overrides`, so a correction missing from the package data is injected before any cell is written.
- The gate refuses a `clear` or `override` of any block finding from `RR_FOOTING` (`OverrideError`); `T12_FOOTING` blocks can still be accepted with a reviewer note. A rent-roll footing failure is resolved by correcting data with a `resolution_strategy`; `review.apply_data_overrides` rejects a data override on that check that lacks the strategy valid for its action (`resolve_footing`: `use_calculated_sum`; `edit_value`: `use_stated_total` or `edit_line_item`).

---

## 9. Excel export (`export/`)

The workbook is built entirely in code (`excel_writer.py`). No template file. Every row and column constant lives in `layout.py`. Workbook properties `created` and `modified` are set to the run's `created_at` so output is stable.

Fonts: inputs blue (`0000FF`), formulas black. Number formats: money `#,##0.00;(#,##0.00)`, percent `0.00%`, ratio `0.00"x"`.

Bounded ranges everywhere. **No whole-column references.**

- Rent roll rows: `2:2001` (2,000 units max; writer raises if exceeded).
- T-12 rows: `2:201` (200 lines max).
- Broker pro forma block on `Assumptions`: rows `21:40`.

### 9.1 Tab `README`

| Cell | Content |
| --- | --- |
| A1 | `cre-screen underwriting export` |
| A3:B3 | `Deal ID`, deal_id |
| A4:B4 | `Run ID`, run_id |
| A5:B5 | `Generated (UTC)`, ISO timestamp |
| A6:B6 | `Model`, model_id |
| A7:B7 | `Gate state`, final_state |
| A8:B8 | `Open block findings`, count |
| A9:B9 | `Open warn findings`, count |
| A10:B10 | `Rent roll source`, file name |
| A11:B11 | `T-12 source`, file name |
| A13 | `Synthetic test data; not a real property.` (only for synthetic deals) |
| A14 | `Blue = input. Black = formula. Thresholds are placeholders; see config/thresholds.yaml.` |

### 9.2 Tab `Assumptions`

Columns: A label, B value, C note. Every row below defines a workbook-scoped named range pointing at its B cell.

| Row | Name | B value | Font | Note |
| --- | --- | --- | --- | --- |
| 2 | `AskingPrice` | `proforma.asking_price` or blank | blue | From proforma.yaml |
| 3 | `EntryCapRate` | `0.0625` | blue | Used only if no asking price |
| 4 | `PurchasePrice` | `=IF(AskingPrice>0,AskingPrice,BrokerNOI/EntryCapRate)` | black | |
| 5 | `LTV` | `0.65` | blue | |
| 6 | `LoanAmount` | `=PurchasePrice*LTV` | black | |
| 7 | `InterestRate` | `0.065` | blue | |
| 8 | `AmortYears` | `30` | blue | |
| 9 | `IOPeriodMonths` | `0` | blue | Year-1 debt service blends IO and amortizing months |
| 10 | `ExpenseGrowth` | `0.03` | blue | Applied to T-12 opex in underwritten column |
| 11 | `MgmtFeePct` | `0.03` | blue | Floor on management fee as % of EGI |
| 12 | `ReservesPerUnit` | `250` | blue | Placeholder; calibrate |
| 13 | `UnitCount` | `=COUNTA(Rent_Roll!$A$2:$A$2001)` | black | |
| 14 | `AssessmentRatio` | blank | blue | Local assessment ratio, if known |
| 15 | `TaxRate` | blank | blue | Local tax rate as a decimal, if known |
| 16 | `ReassessedTaxBump` | `1.15` | blue | Fallback only; arbitrary placeholder |
| 17 | `ReassessedTax` | `=IF(AND(AssessmentRatio>0,TaxRate>0),PurchasePrice*AssessmentRatio*TaxRate,T12Taxes*ReassessedTaxBump)` | black | |
| 20 | (header) | `Broker pro forma (annual, from proforma.yaml)` | | |
| 21:40 | | A = bucket name, B = amount (blue), C = `source_note` | blue | One row per pro forma line, in COA order |

Named ranges defined on other tabs: `BrokerNOI = ProForma_NOI_DSCR!$D$24`, `T12Taxes = ProForma_NOI_DSCR!$B$13`.

Why `AskingPrice` comes first: pricing the deal off the broker's own NOI bakes the broker's numbers into the loan size. The cap-rate fallback exists only for unpriced deals.

### 9.3 Tab `Audit_Inputs`

Columns: A Item, B Value, C Source file, D Location, E Raw text, F Provenance. Every B cell gets a cell comment: `Source: <citation.short()>\nRaw: <raw_text>\nVia: <provenance>`, author `cre-screen`.

Rows: rent roll stated totals, every `SubtotalRow`, every T-12 `StatedSubtotal` total, as-of date, T-12 period end, then every value referenced by an open or overridden finding.

### 9.4 Tab `Rent_Roll`

Header row 1. Data rows 2:2001.

| Col | Field | Notes |
| --- | --- | --- |
| A | Unit | |
| B | Unit Type | |
| C | Beds | |
| D | Baths | |
| E | SqFt | |
| F | Status | lowercase status literal |
| G | In-Place Rent | blue; cell comment with citation |
| H | Market Rent | blue; cell comment with citation |
| I | Lease Start | date |
| J | Lease End | date |
| K | Concession Monthly Equiv | blue |
| L | Effective Rent | `=G{r}-K{r}` |
| M | Flags | comma-separated check IDs touching this unit |
| N | Source | `citation.short()` of the unit row |

Formatted as an Excel Table named `RentRollTable` over `A1:N{last}`.

### 9.5 Tab `T12_Mapped`

Header row 1. Data rows 2:201.

| Col | Field | Notes |
| --- | --- | --- |
| A | Label | |
| B | Bucket | COA bucket name |
| C | Include_In_UW | `1` or `0` (value, from `include_in_uw`) |
| D | Nonrecurring | `1` or `0` |
| E:P | M01..M12 | blue; header row shows the document's month labels |
| Q | Total | `=SUM(E{r}:P{r})` |
| R | Stated Total | blue; cell comment with citation |
| S | T3_Annualized | `=SUM(N{r}:P{r})*4` |
| T | Mapping Source | |
| U | Source | `citation.short()` |

### 9.6 Tab `Findings`

Columns: A Check, B Severity, C Status, D Message, E Delta, F Units / Lines, G Citations (`; `-joined `short()`), H Reviewer Note. Sorted block, warn, info, then check ID.

### 9.7 Tab `ProForma_NOI_DSCR` (formulas only)

Columns: A Line, B T-12 Actual, C T-3 Annualized, D Broker Pro Forma, E Underwritten, F Notes. Row 2 holds the column headers. Every numeric cell in `B4:E37` is a formula.

Formula templates, where `{X}` is the row's bucket name and ranges are absolute:

- `Q = T12_Mapped!$Q$2:$Q$201`, `S = T12_Mapped!$S$2:$S$201`, `BKT = T12_Mapped!$B$2:$B$201`, `INC = T12_Mapped!$C$2:$C$201`, `PFV = Assumptions!$B$21:$B$40`, `PFB = Assumptions!$A$21:$A$40`.
- B (T-12): `=SUMIFS(Q,BKT,"{X}")`
- C (T-3 annualized): `=SUMIFS(S,BKT,"{X}")`
- D (Broker): `=SUMIFS(PFV,PFB,"{X}")`

| Row | A | E (Underwritten) |
| --- | --- | --- |
| 3 | `Revenue` (section label) | |
| 4 | GROSS_POTENTIAL_RENT | `=SUMIFS(S,BKT,"GROSS_POTENTIAL_RENT",INC,1)` |
| 5 | LOSS_TO_LEASE | `=SUMIFS(S,BKT,"LOSS_TO_LEASE",INC,1)` |
| 6 | VACANCY_LOSS | `=SUMIFS(S,BKT,"VACANCY_LOSS",INC,1)` |
| 7 | CONCESSIONS | `=SUMIFS(S,BKT,"CONCESSIONS",INC,1)` |
| 8 | BAD_DEBT | `=SUMIFS(S,BKT,"BAD_DEBT",INC,1)` |
| 9 | UTILITY_REIMBURSEMENT | `=SUMIFS(Q,BKT,"UTILITY_REIMBURSEMENT",INC,1)` |
| 10 | OTHER_INCOME | `=SUMIFS(Q,BKT,"OTHER_INCOME",INC,1)` |
| 11 | Effective Gross Income | `=SUM(E4:E10)` (B, C, D likewise) |
| 12 | `Operating expenses` (section label) | |
| 13 | REAL_ESTATE_TAXES | `=MAX(SUMIFS(Q,BKT,"REAL_ESTATE_TAXES",INC,1),ReassessedTax)` |
| 14 | INSURANCE | `=SUMIFS(Q,BKT,"INSURANCE",INC,1)*(1+ExpenseGrowth)` |
| 15 | UTILITIES | same pattern |
| 16 | REPAIRS_MAINTENANCE | same pattern |
| 17 | TURNOVER | same pattern |
| 18 | CONTRACT_SERVICES | same pattern |
| 19 | PAYROLL | same pattern |
| 20 | MANAGEMENT_FEE | `=MAX(SUMIFS(Q,BKT,"MANAGEMENT_FEE",INC,1)*(1+ExpenseGrowth),E11*MgmtFeePct)` |
| 21 | GENERAL_ADMIN | same pattern as 14 |
| 22 | MARKETING | same pattern as 14 |
| 23 | Total Operating Expenses | `=SUM(E13:E22)` (B, C, D likewise) |
| 24 | Net Operating Income | `=E11-E23` (B, C, D likewise) |
| 25 | Replacement Reserves | B: `=SUMIFS(Q,BKT,"CAPEX_RESERVES")`, C: `=SUMIFS(S,BKT,"CAPEX_RESERVES")`, D: `=SUMIFS(PFV,PFB,"CAPEX_RESERVES")`, E: `=ReservesPerUnit*UnitCount` |
| 26 | Net Cash Flow | `=E24-E25` (B, C, D likewise) |
| 27 | `Valuation and debt` (section label) | |
| 28 | Purchase Price | `=PurchasePrice` in B:E |
| 29 | Cap Rate on NOI | `=IFERROR(E24/E28,0)` (B, C, D likewise) |
| 30 | Loan Amount | `=LoanAmount` in B:E |
| 31 | Debt Service (Year 1) | `=MIN(IOPeriodMonths,12)*LoanAmount*InterestRate/12+(12-MIN(IOPeriodMonths,12))*-PMT(InterestRate/12,AmortYears*12,LoanAmount)` in B:E |
| 32 | DSCR (on NCF) | `=IFERROR(E26/E31,0)` (B, C, D likewise) |
| 33 | Debt Yield | `=IFERROR(E24/E30,0)` (B, C, D likewise) |
| 34 | `Operations` (section label) | |
| 35 | Physical Occupancy | E: `=IFERROR((COUNTIFS(Rent_Roll!$F$2:$F$2001,"occupied")+COUNTIFS(Rent_Roll!$F$2:$F$2001,"notice"))/UnitCount,0)`; B:D `=E35` |
| 36 | Economic Occupancy | B: `=IFERROR(SUM(B4:B8)/B4,0)`; C: `=IFERROR(SUM(C4:C8)/C4,0)`; D: `=IFERROR(SUM(D4:D8)/D4,0)`; E: `=IFERROR(SUM(E4:E8)/E4,0)` |
| 37 | Monthly Loss to Lease | E: `=SUMIFS(Rent_Roll!$H$2:$H$2001,Rent_Roll!$F$2:$F$2001,"occupied")+SUMIFS(Rent_Roll!$H$2:$H$2001,Rent_Roll!$F$2:$F$2001,"notice")-SUMIFS(Rent_Roll!$G$2:$G$2001,Rent_Roll!$F$2:$F$2001,"occupied")-SUMIFS(Rent_Roll!$G$2:$G$2001,Rent_Roll!$F$2:$F$2001,"notice")`; B:D `=E37` |

The debt-service formula handles 0, partial, and 12+ IO months in one expression. It was verified with `formulas` 1.3.4 against a Python PMT for IO = 0, 6 and 24 months.

### 9.8 `uw_math.py`

A pure-Python `Decimal` implementation of every value in `ProForma_NOI_DSCR!B4:E37` given the same `DealPackage` and default assumptions. Function: `compute_uw(pkg: DealPackage, assumptions: Assumptions) -> dict[str, Decimal]` keyed by cell address (e.g. `"E24"`). PMT uses the standard annuity formula. This is the "Python side" of the Excel-agrees-with-Python test.

---

## 10. Streamlit UI (`app/streamlit_app.py`)

Run: `uv run streamlit run src/cre_screen/app/streamlit_app.py`.

- **Sidebar:** deal picker listing folders under `data/synthetic/` (dev only; held-out deals hidden unless `CRE_SHOW_HELDOUT=1`) and `data/real/`; file uploader that writes to `out/uploads/<timestamp>/`; as-of date input; Run button; cache mode shown read-only; cost of this run and lifetime ledger total.
- **Findings tab:** `st.dataframe` of findings sorted block, warn, info, with `on_select="rerun"` and `selection_mode="single-row"`. Selecting a row sets `st.session_state["focus"]` to its first citation.
- **Audit view tab:** two columns. Left: the focused page rendered by `render.render_page_png` with every citation box on that page drawn by `render.draw_boxes`, focused box in a stronger color. Right: extracted rows for that page. Scanned pages show no boxes and a label "values transcribed by vision model".
- **T-12 mapping tab:** `st.data_editor` with a `SelectboxColumn` of the 20 buckets. Changing a bucket requires a note (min length from thresholds) and creates a `remap_line` override.
- **Summary tab:** physical occupancy, economic occupancy, loss to lease, NOI from T-12 to underwritten (values from `uw_math`).
- **Export:** `st.download_button` with the workbook bytes. `disabled=True` while `open_blocks()` is not empty, with the count shown next to it.
- Pipeline results cached with `st.cache_data` keyed by deal folder hash plus overrides hash. No UI interaction triggers an LLM call except the Run button.

---

## 11. Synthetic data and evaluation

### 11.1 Generator (`synth/`)

`synth/params/base.yaml` (owner fills from Philadelphia multifamily metrics; only aggregate ranges are committed):

```yaml
unit_count: {min: 24, max: 96}
unit_mix:                       # share of units, must sum to 1
  studio: 0.10
  "1x1": 0.50
  "2x1": 0.15
  "2x2": 0.20
  "3x2": 0.05
sq_ft: {studio: [450, 550], "1x1": [650, 800], "2x1": [850, 950], "2x2": [950, 1150], "3x2": [1200, 1400]}
market_rent_per_sf: {min: "1.45", max: "2.10"}
inplace_vs_market: {min: "0.90", max: "1.00"}   # per unit
rent_noise_within_type: "0.08"                  # keeps base deals below rr_rent_outlier threshold
physical_vacancy: {min: "0.04", max: "0.09"}
model_or_employee_units: {max: 1}
other_income_per_unit_month: {min: "25", max: "75"}
opex_per_unit_year:             # ranges; owner calibrates
  REAL_ESTATE_TAXES: ["1400", "2600"]
  INSURANCE: ["550", "900"]
  UTILITIES: ["900", "1600"]
  REPAIRS_MAINTENANCE: ["500", "900"]
  TURNOVER: ["250", "450"]
  CONTRACT_SERVICES: ["200", "400"]
  PAYROLL: ["1200", "2000"]
  GENERAL_ADMIN: ["200", "400"]
  MARKETING: ["80", "200"]
mgmt_fee_pct_of_egi: {min: "0.03", max: "0.05"}
capex_per_unit_year: ["200", "400"]
```

Generation steps (`python -m synth.generate`):

1. `DealTruth` from params with `random.Random(seed + deal_number)` and `Faker().seed_instance(seed + deal_number)`.
2. T-12 built from the rent roll so base deals reconcile: monthly GPR = sum of market rents; vacancy = −market rent of vacant/down units; loss to lease = −(market − in-place) on occupied units; months 1 to 9 scaled down by 0 to 3% for rent growth. Taxes paid in two installments (months 4 and 10); insurance paid once (month 1).
3. Apply traps (11.2). Each trap mutates the document view and appends `ExpectedFinding`s.
4. Render with the deal's layout family. Set `reportlab.rl_config.invariant = 1` before rendering so PDFs carry no timestamps (verify the flag on install; if absent, compare extracted text instead of bytes). Excel files get fixed workbook properties.
5. Degrade if `scanned` (11.3).
6. Write `truth.json` (`model_dump_json(indent=2)`), `meta.json`, `proforma.yaml`.

Base-deal constraint: for every check without a planted trap, the generator's own arithmetic keeps the check's metric at most half of its threshold (e.g. RR vs T-3 gap under 2.5%). `tests/test_synth.py` asserts this from `truth.json` values using simple arithmetic, not by running the checks.

### 11.2 Trap catalog

| Trap ID | Plant | Expected check |
| --- | --- | --- |
| `TRAP_RR_FOOTING` | Stated grand total with two adjacent digits transposed (delta at least $9) | `RR_FOOTING` |
| `TRAP_RR_ZERO_RENT` | 1 to 2 occupied units with rent 0 and no model/employee status | `RR_ZERO_RENT_OCC` |
| `TRAP_RR_EXPIRED_LEASE` | 2 to 5 occupied units with lease end 1 to 9 months before as-of | `RR_EXPIRED_LEASE` |
| `TRAP_RR_DUP_UNIT` | One unit printed again as the first row after a page break | `RR_DUP_UNIT` |
| `TRAP_RR_HIDDEN_CONCESSION` | 15 to 30% of units have "1 mo free on 12 mo lease" or "$100 off/mo" in the notes column; rent shown gross | `RR_CONCESSION` |
| `TRAP_X_RENT_PUSH` | In-place rents raised 8 to 15% versus the T-12's last 3 months | `X_RR_VS_T3` |
| `TRAP_T12_FOOTING` | One month cell of one expense line changed; stated total unchanged | `T12_FOOTING` |
| `TRAP_T12_ONE_TIME_INCOME` | Line "Insurance Proceeds" or "Lease Buyout Fee" in other income, one month only | `T12_NONRECURRING` |
| `TRAP_T12_SPIKE` | One month of R&M at 4 to 6 times its median | `T12_SPIKE` |
| `TRAP_PF_TAX_DROP` | Pro forma taxes 10 to 25% below T-12 | `PF_TAX_DROP` |
| `TRAP_PF_INS_DROP` | Pro forma insurance 20 to 35% below T-12 | `PF_INS_DROP` |
| `TRAP_PF_VACANCY` | Pro forma vacancy 2 to 3% while physical vacancy is 6 to 9% | `PF_VACANCY` |
| `TRAP_PF_MGMT_FEE` | Pro forma management fee 2% of EGI while T-12 runs 4 to 5% | `PF_MGMT_FEE` |

Held-out variants (used only for deals 15 to 20; code lives in `traps.py` behind `variant="heldout"`):

| Trap ID | Held-out variant |
| --- | --- |
| `TRAP_RR_FOOTING` | A unit row is omitted from the printed table but included in the stated total |
| `TRAP_RR_DUP_UNIT` | Duplicate appears on the same page, not adjacent |
| `TRAP_RR_HIDDEN_CONCESSION` | Concession appears only in a footnote listing unit numbers |
| `TRAP_T12_ONE_TIME_INCOME` | Label is ambiguous: "Other Income - Misc Adj" |
| `TRAP_PF_TAX_DROP` | Drop accompanied by a note "tax appeal pending" in `source_note` |

Deal plan:

| Deals | Split | Traps per deal | Notes |
| --- | --- | --- | --- |
| 001, 002 | dev | 0 | Clean controls |
| 003 to 014 | dev | 2 to 4 | Every trap ID appears at least twice across dev |
| 015 to 019 | heldout | 2 to 4 | Every held-out variant appears at least once |
| 020 | heldout | 0 | Clean control |

Seeds: dev `7`, held-out `1015`. Formats: 4 dev and 2 held-out rent rolls are `.xlsx`; 2 dev and 2 held-out rent rolls are scanned; 4 dev and 2 held-out T-12s are `.xlsx`.

### 11.3 Layout families and degradation

Rent roll: `rr_classic` (one table, header repeated each page), `rr_grouped` (units grouped by type with subtotal rows), `rr_wide` (extra columns: deposit, balance, move-in, tenant name), `rr_excel` (title rows, merged header cells, blank spacer rows). T-12: `t12_grid` (12 months + total), `t12_sections` (Income/Expenses sections with subtotals and NOI), `t12_excel`.

Messiness applied to every family: header synonyms vary per deal; money formats vary (`$1,250.00`, `1250`, `(125.00)`); date formats vary; footnotes; blank spacer rows.

`degrade.py`: rasterize with `render.rasterize_pdf` at 150 dpi, rotate by a seeded angle in ±1.5°, add Gaussian noise (sigma 6), save as JPEG quality 70, rewrap as image-only PDF.

M1 ships `rr_classic`, `t12_grid` only. M6 adds the rest.

### 11.4 Scoring (`evals/scoring.py`)

- Units matched by normalized unit ID. Numeric fields scored: `beds`, `baths`, `sq_ft`, `in_place_rent`, `market_rent` (exact equality), plus `lease_start`, `lease_end` (exact date). Accuracy counts against **document-view** truth.
- `units_found` = truth units with a matching extracted unit; `phantom_units` = extracted units with no truth match (duplicates count once as truth).
- COA dollar-weighted accuracy: for each truth line matched by label, `abs(stated_total)` counts as correct if the extracted bucket equals `true_bucket`.
- A finding matches an expected finding if `check_id` equal AND (expected has no unit IDs or line labels, OR they overlap).
- `caught`: expected findings matched by any finding (any severity).
- `false_positives`: warn or block findings matching no expected finding. Info findings never count as false positives because they make no claim of a problem.
- `clean_control_findings`: warn or block findings on deals with no traps.
- Report every metric split by `split` and by provenance (`text_layer`, `excel`, `llm_vision`).

### 11.5 Real sanity set

`data/real/<name>/truth_manual.yaml` (gitignored with the rest of `data/real/`):

```yaml
source_url: "https://..."            # where the public document came from
retrieved: "2026-11-15"
units_spot_checked:                  # 15 units chosen by the owner, values as printed
  - {unit_id: "101", in_place_rent: "1250.00", market_rent: "1300.00"}
stated_totals: {in_place_rent: "56250.00"}
t12_lines_spot_checked:
  - {label: "Real Estate Taxes", bucket: REAL_ESTATE_TAXES, total: "98450.00"}
findings_owner_agrees_with: []       # check IDs the owner confirms after reading the docs
manual_entry_minutes: null           # owner's timed baseline for keying this deal into Excel
review_minutes: null                 # owner's time to clear findings in the UI
```

`evals/manual_timing.yaml` copies the three timing pairs so the committed report can show the baseline without committing the documents.

### 11.6 Held-out lock

- `data/synthetic/HELDOUT_MANIFEST.sha256` lists `sha256  relative/path` for every file in deals 015 to 020. Written once by `python -m synth.generate --split heldout`.
- `tests/test_heldout_lock.py` recomputes and compares. The generator refuses to overwrite held-out folders without `--force-heldout`.
- Before the M8 final run, the only allowed `src/` changes are bug fixes found on dev or real deals. After the final run, results are recorded as-is. Any later fix is reported in the README as "post-hoc" with both numbers.

### 11.7 Eval CLI and reports

`python -m evals.run_eval --split {dev,heldout,real,all} [--cache-mode replay|record|off] [--model ID] [--run-cap USD] [--final] [--out evals/reports]`

- `--split heldout` requires `--final`. A final run writes `evals/reports/FINAL_<date>_<sha>.md` and refuses to run again if one exists, unless `--allow-second-final` (which the report then states).
- Every run writes `evals/reports/<UTC date>_<git short sha>_<split>.json` (list of `DealScore`) and a `.md` with the aggregate table and every miss.
- `evals/test_regression.py`: loads `evals/baseline_dev.json`; runs the dev split in replay mode; fails if caught count drops, false positives rise, or numeric accuracy drops by more than 0.5 percentage points. `--update-baseline` on `run_eval` rewrites the baseline (owner only).

---

## 12. Milestones

Each milestone ends when every acceptance command passes on the owner's Windows machine **and** in CI. Claude Code stops at each exit and reports. Commands are PowerShell-compatible.

### M0: Skeleton

Deliverables: `pyproject.toml`, `uv.lock`, `.gitignore`, `LICENSE`, README stub, all four config files exactly as Section 5, `config.py`, every schema file exactly as Section 6, `mapping/cost.py` (`CostTracker`, ledger), `mapping/cache.py`, `tests/fakes.py`, CI workflow.

CI (`.github/workflows/ci.yml`): matrix `os: [windows-latest, ubuntu-latest]`, `python: ["3.11", "3.12"]`; steps: checkout, `astral-sh/setup-uv`, `uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`. Env `CRE_LLM_CACHE_MODE=replay`, `PYTHONUTF8=1`. No `ANTHROPIC_API_KEY` secret is configured.

Tests: `tests/test_schemas.py` (round-trip every model to JSON and back; Citation location rules; HeaderMap one-to-one errors; T12Line length 12; bucket tuple sizes; BrokerProForma rejects control buckets; Money rejects 3 decimal places), `tests/test_config.py` (all four files load; coa.yaml has the 20 buckets in order; unknown key fails), `tests/test_cost_tracker.py` (cost math for Haiku short and long context and Sonnet; per-run and lifetime caps raise; ledger appends valid JSON lines), `tests/test_llm_cache.py` (key stability; replay miss raises; record writes atomically; hit returns validated model).

Acceptance:

```powershell
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/test_schemas.py tests/test_config.py tests/test_cost_tracker.py tests/test_llm_cache.py -q
```

Exit: all pass; CI green on all four matrix cells.

### M1: Generator v1

Deliverables: `synth/` with `rr_classic`, `t12_grid`, `truth.py`, `traps.py` (all 13 dev traps), `generate.py`. Deals 001 to 005 generated and committed (PDF only, no scans).

Tests: `tests/test_synth.py` (generating twice with seed 7 yields identical `truth.json` bytes and identical pdfplumber-extracted text; every expected finding names a valid check ID; base-deal constraint from 11.1 holds for each deal; clean controls have zero expected findings; units' stated total equals the sum of printed rents unless `TRAP_RR_FOOTING` is present).

Acceptance:

```powershell
uv run python -m synth.generate --split dev --deals 1-5 --seed 7 --out data/synthetic
uv run pytest tests/test_synth.py -q
```

Exit: all pass; owner opens `deal_003/rent_roll.pdf` and confirms it reads like a rent roll.

### M2: Rent roll path

Deliverables: `ingest/` (all modules), `mapping/header_mapper.py`, `mapping/concessions.py`, `extract/rent_roll.py`, `reconcile/checks/rr_footing.py`, CLI `extract-rr`.

Tests: `tests/test_parse.py` (Hypothesis: for any Decimal in ±10,000,000 with 2 places and each formatter `plain`, `commas`, `dollar_commas`, `parens_negative`, `trailing_minus`, `parse_money(fmt(x)) == x`; accounting dash is zero; garbage raises; date formats; month-only end-of-month), `tests/test_pdf_tables.py` (column assignment on a hand-built two-page fixture with a repeated header), `tests/test_header_mapper.py` (synonym path makes zero LLM calls via `FakeLLMClient`; LLM path fills missing headers as `ignore`; duplicate mapping raises), `tests/test_concessions.py` (regex path; grounding pass and fail; monthly-equivalent math), `tests/test_rent_roll_extract.py` (on deals 001 to 005: every truth unit found, zero phantoms, money fields exact), `tests/test_check_rr_footing.py` (tolerance edge at exactly $1.00 passes and $1.01 fails; occupied-only basis passes; missing stated total gives info).

Acceptance:

```powershell
uv run pytest tests/test_parse.py tests/test_pdf_tables.py tests/test_header_mapper.py tests/test_concessions.py tests/test_rent_roll_extract.py tests/test_check_rr_footing.py -q
uv run python -m cre_screen extract-rr data/synthetic/deal_003
```

Exit: all pass; `extract-rr` prints unit count, stated vs computed total, and the footing result.

### M3: T-12 path

Deliverables: `extract/t12.py`, `extract/proforma.py`, `mapping/coa_mapper.py`, `mapping/llm_client.py` (`AnthropicLLMClient`), prompts, CLI `extract-t12`. First recorded cache entries for deals 001 to 005.

Tests: `tests/test_t12_extract.py` (lines, months and stated totals exact on deals 001 to 005; subtotal rows become `StatedSubtotal`; sign normalization flag), `tests/test_coa_mapper.py` (exact and fuzzy paths make no LLM call; LLM batch path with `FakeLLMClient`; missing labels re-asked once then `UNMAPPED`; nonrecurring keyword detection), `tests/test_proforma_load.py` (YAML strings load as Decimal; control buckets rejected), `tests/test_llm_client.py` (with a fake Instructor client: cache key includes schema; budget guard raises before any call; every attempt recorded).

Acceptance:

```powershell
uv run pytest tests/test_t12_extract.py tests/test_coa_mapper.py tests/test_proforma_load.py tests/test_llm_client.py -q
$env:CRE_LLM_CACHE_MODE = "record"
uv run python -m cre_screen extract-t12 data/synthetic/deal_003
$env:CRE_LLM_CACHE_MODE = "replay"
uv run python -m cre_screen extract-t12 data/synthetic/deal_003
```

Exit: all pass; the second `extract-t12` run reports cost $0.00 and identical mappings; `.cost_ledger.jsonl` shows the recorded spend.

### M4: Full gate

Deliverables: all 15 checks, `reconcile/gate.py`, `reconcile/reread.py`, `reconcile/metrics.py`, `pipeline.py`, CLI `run`, `evals/` (scoring, report, `run_eval.py`), deals 006 to 014 generated (still `rr_classic`/`t12_grid`), dev cache recorded, `evals/baseline_dev.json`.

Tests: one file per check, `tests/test_check_<check_id_lower>.py`, each with at least: a passing case, a failing case, the exact threshold edge, and missing-input behavior. `tests/test_gate.py` (transition sequence for: clean deal; footing fails and re-read agrees, so cause "document"; re-read disagrees, so cause "extraction"; open block gives REVIEW; override clears to READY), `tests/test_scoring.py` (matching rule; info excluded from false positives; dollar-weighted COA).

Acceptance:

```powershell
uv run python -m synth.generate --split dev --deals 6-14 --seed 7 --out data/synthetic
uv run pytest tests -q
$env:CRE_LLM_CACHE_MODE = "record"
uv run python -m evals.run_eval --split dev
$env:CRE_LLM_CACHE_MODE = "replay"
uv run python -m evals.run_eval --split dev --update-baseline
uv run pytest evals/test_regression.py -q
```

Exit: all pass; the dev report exists; clean controls 001 and 002 have zero warn/block findings.

### M5: Excel export

Deliverables: `export/layout.py`, `export/excel_writer.py`, `export/uw_math.py`, CLI `export`.

Tests: `tests/test_excel_export.py`:

- `test_no_hardcoded_outputs`: every cell in `ProForma_NOI_DSCR!B4:E37` that is not a section label row is a string starting with `=`; same for `Assumptions!B4`, `B6`, `B13`, `B17`; and `Rent_Roll!L`, `T12_Mapped!Q`, `T12_Mapped!S` data rows.
- `test_no_whole_column_refs`: no formula in the workbook matches `![A-Z]+:\$?[A-Z]+(?![0-9$])` or `\$[A-Z]+:\$[A-Z]+(?![0-9])`.
- `test_named_ranges`: all 16 names in Section 9.2 plus `BrokerNOI`, `T12Taxes` exist and point at the specified cells.
- `test_excel_matches_python`: for deals 001, 003, 007, export, recalculate with `formulas.ExcelModel().loads(path).finish().calculate()`, and assert every value in `B4:E37` equals `uw_math.compute_uw` within $0.01 (money) or 1e-6 (ratios).
- `test_io_blend`: with `IOPeriodMonths` set to 0, 6, and 24, recalculated debt service equals the Python value.
- `test_citation_comments`: every `Audit_Inputs!B` data cell and every `Rent_Roll!G` data cell has a comment containing `Source:`.

Acceptance:

```powershell
uv run pytest tests/test_excel_export.py -q
uv run python -m cre_screen run data/synthetic/deal_003 --export out/deal_003.xlsx --allow-open-blocks
```

Exit: all pass; owner opens `out/deal_003.xlsx` in Excel and sees no `#REF!`, `#NAME?` or `#DIV/0!` errors.

### M6: Generator v2, scans, held-out

Deliverables: layouts `rr_grouped`, `rr_wide`, `rr_excel`, `t12_sections`, `t12_excel`; `degrade.py`; held-out trap variants; `mapping/vision.py`. Regenerate dev 001 to 014 with mixed layouts per 11.2 (dev cache re-recorded, baseline updated). Generate held-out 015 to 020 and the manifest. Do not run the pipeline on held-out deals.

Tests: `tests/test_synth_layouts.py` (each family renders, extracts with zero phantoms on a seeded sample), `tests/test_vision.py` (FakeLLMClient returns a `VisionTablePage`; every citation has `provenance="llm_vision"`; strings parsed by code), `tests/test_heldout_lock.py` (manifest matches files).

Acceptance:

```powershell
uv run python -m synth.generate --split dev --deals 1-14 --seed 7 --out data/synthetic --overwrite-dev
uv run python -m synth.generate --split heldout --deals 15-20 --seed 1015 --out data/synthetic
uv run pytest tests/test_synth.py tests/test_synth_layouts.py tests/test_vision.py tests/test_heldout_lock.py -q
$env:CRE_LLM_CACHE_MODE = "record"
uv run python -m evals.run_eval --split dev
$env:CRE_LLM_CACHE_MODE = "replay"
uv run python -m evals.run_eval --split dev --update-baseline
uv run pytest -q
```

Exit: all pass; held-out manifest committed; held-out folders never opened by the pipeline.

### M7: Streamlit UI

Deliverables: `app/streamlit_app.py` per Section 10; CLI `app` that launches it.

Tests: `tests/test_app.py` using `streamlit.testing.v1.AppTest`: app loads with deal 003 selected in replay mode; findings table renders; export button disabled while blocks are open; a valid override note enables export; a short note is rejected.

Acceptance:

```powershell
uv run pytest tests/test_app.py -q
uv run streamlit run src/cre_screen/app/streamlit_app.py
```

Exit: tests pass; owner clears all findings on deal 003 by hand and downloads the workbook.

### M8: Ship

Deliverables: 3 real deals in `data/real/` with `truth_manual.yaml` and timing; final held-out run; Sonnet comparison; README with results; demo GIF; resume numbers.

Acceptance:

```powershell
$env:CRE_LLM_CACHE_MODE = "record"
uv run python -m evals.run_eval --split real
uv run python -m evals.run_eval --split heldout --final
uv run python -m evals.run_eval --split all --model claude-sonnet-5-5 --run-cap 2.50 --out evals/reports/sonnet
$env:CRE_LLM_CACHE_MODE = "replay"
uv run pytest -q
uv run python -m cre_screen budget
```

Exit: all pass; `budget` shows lifetime spend under $10.00; README results table filled only from report files; final held-out report committed unchanged.

---

## 13. CLI reference

`python -m cre_screen <command>` (argparse subcommands):

| Command | Arguments | Does |
| --- | --- | --- |
| `extract-rr` | `DEAL_DIR [--as-of YYYY-MM-DD] [--sheet NAME]` | Prints units found, stated vs computed totals, footing result |
| `extract-t12` | `DEAL_DIR [--sheet NAME]` | Prints lines, bucket per line with mapping source, cost |
| `run` | `DEAL_DIR [--as-of] [--export PATH] [--allow-open-blocks] [--model ID] [--run-cap USD]` | Full pipeline; writes `out/<deal>/package.json`; export refused while blocks are open unless flagged |
| `export` | `PACKAGE_JSON --out PATH` | Workbook from a saved package |
| `budget` | none | Lifetime spend from ledger, by model and task, and remaining under the cap |
| `app` | none | Launches Streamlit |

Exit codes: 0 success, 2 bad input, 3 budget exceeded, 4 cache miss in replay mode, 5 open blocks on export.

`python -m synth.generate --split {dev,heldout} --deals A-B --seed N --out DIR [--overwrite-dev] [--force-heldout]`

`python -m evals.run_eval` per Section 11.7.

---

## 14. Prompt templates (`mapping/prompts/`)

Templates use `str.format` with named fields. All four start with the same system line: `You classify labels for a commercial real estate underwriting tool. You never invent numbers. Return only the requested structure.`

**`header_map.md`** (user content):

```text
Map each column header from a multifamily rent roll to exactly one field.
Allowed fields: {fields_with_descriptions}
Use "ignore" for any column that is none of these.
Each field except "ignore" may be used at most once.
Return every header exactly once, spelled exactly as given.

Headers:
{headers_json}

First rows (raw text, for context only):
{sample_rows_json}
```

**`coa_map.md`**:

```text
Map each T-12 operating statement line label to one chart-of-accounts bucket.
Buckets:
{buckets_with_descriptions}
Rules:
- Use UNMAPPED if you are unsure. A human reviews UNMAPPED lines.
- Set is_nonrecurring true only for one-time items (insurance proceeds, settlements, lease buyouts, refunds).
- Debt service, interest, depreciation and owner costs are NON_OPERATING.
- Return every label exactly once, spelled exactly as given. Keep rationale under 160 characters.

Labels:
{labels_json}
```

**`concession.md`**:

```text
Read this rent roll concession note and describe the concession.
Only use numbers that appear in the note. If a quantity is not stated, return null.
Note: {note}
```

**`vision_page.md`**:

```text
This image is one page of a rent roll table. Transcribe it.
Return the header texts exactly as printed and every row.
For each row, give each cell's text exactly as printed, including $ signs, commas and parentheses.
Use an empty string for a blank cell. Do not compute, correct or total anything.
Mark rows that are subtotals, repeated headers or footnotes with the matching row_kind.
Also return every title line printed above the table (property name, report title, the "As of" date) as its own row with row_kind other, in reading order, with the line's exact text as the value of its first cell.
```

**`vision_t12.md`** (used only when a text-layer T-12 cannot be read):

```text
This image is one page of a trailing 12 month income and expense statement (a T-12). Transcribe it.
Return the header texts exactly as printed, left to right. If the column of line item names has no printed header, give it the header "Line Item". The month columns and a Total column follow.
For each row, give each cell's text exactly as printed, including $ signs, commas and parentheses.
Use an empty string for a blank cell. Do not compute, correct or total anything.
Mark every line item row with row_kind unit. Mark total rows such as Total Income, Total Expenses and NOI with row_kind subtotal. Mark section titles and notes with row_kind footnote.
Also return every title line printed above the table (property name, report title, the period) as its own row with row_kind other, in reading order, with the line's exact text as the value of its first cell.
```

---

## 15. Definition of done (project)

- CI green on Windows and Ubuntu.
- `evals/reports/FINAL_*.md` committed with held-out results, every miss explained in one line.
- Sonnet comparison report committed.
- README: problem, architecture diagram, design invariants, results table (counts and rates, split by dev, held-out, real, and provenance), budget spent, known limitations, AGPL note, "thresholds are placeholders" note, and the owner's measured manual baseline.
- Lifetime spend in `.cost_ledger.jsonl` under $10.00.
