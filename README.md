# cre-screen

A multifamily deal-screening engine. It reads a rent roll and a T-12 (text PDF, scanned PDF or
Excel), cites every number back to the page and box it came from, maps T-12 lines onto a fixed
chart of accounts, runs a deterministic reconciliation gate, and exports an Excel workbook with
live formulas. A Streamlit app lets a reviewer audit, correct and sign off before anything is
exported.

The model never produces a number that lands in a typed record. It maps header and line labels,
reads concession notes, and transcribes pages that are only images. Every figure is parsed by
code, and every check is a pure Python function. `SPEC.md` is the build contract.

## Results

All numbers below are copied from the report files in `evals/reports/`. Model: `claude-haiku-5-5`.

| Split | Deals | Numeric accuracy | Unit recall | Phantom units | COA $ accuracy | Planted errors caught | False positives | Findings on clean-control deals |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Dev (`evals/reports/haiku/2026-11-07_88e9dcc_dev.md`) | 14 | 1.0000 | 1.0000 | 0 | 1.0000 | 36 / 36 | 0 | 0 |
| **Held-out, final run** (`evals/reports/FINAL_2026-11-07_710445b.md`) | 6 | 1.0000 | 1.0000 | 0 | 0.9981 | **13 / 15** | **2** | 0 |

**Read these two rows differently.** The dev deals were used to build and tune the system, so
100% on dev shows regression safety, not generalization. The held-out deals (015 to 020) were
generated and hash-locked before the final code was finished, and were run exactly once, so that
row is the honest measure. It is not perfect:

| Held-out deal | Provenance | Caught | Result |
| --- | --- | --- | --- |
| 015 | text layer | 3 / 3 | clean |
| 016 | scanned (vision) | 2 / 3 | missed `TRAP_T12_ONE_TIME_INCOME` (no `T12_NONRECURRING` finding); 2 false positives; COA dollars 2,330,825.60 of 2,351,751.78 |
| 017 | Excel | 2 / 3 | missed `TRAP_RR_HIDDEN_CONCESSION` (no `RR_CONCESSION` finding) |
| 018 | scanned (vision) | 3 / 3 | clean |
| 019 | Excel | 3 / 3 | clean |
| 020 | text layer, no traps | 0 / 0 | clean control, 0 findings |

Extraction was exact on all six held-out deals (every numeric field, every unit). The two misses
and the two false positives are in the gate and the label mapping, not in extraction. The
results are recorded as they came out. The held-out deals were not opened to diagnose the
misses, because looking at them would end their use as a test. Any fix made from here on is
**post-hoc** and must be reported with both numbers.

**Cost.** The held-out run cost $0.0159 (two scanned deals read by the model, four read without
it). Everything built and run in this repository, including the Sonnet comparison and the
real-deal tests, cost $0.2597 of a $10.00 budget (`python -m cre_screen budget`).

**Model comparison (dev split).** Sonnet 5.5 and Haiku 5.5 scored identically on all 14 dev deals
(`evals/reports/sonnet/` and `evals/reports/haiku/`). A full Sonnet pass costs $0.0989 against
about $0.02 for Haiku, almost all of it the six scanned pages, so Haiku is the default.

**Real documents.** Two real offering packages are kept out of the repository (`data/real/` is
gitignored). They have no hand-scored truth file yet, so they are reported as outcomes, not
scored:

- `deal_a` (a 16-unit garden property, rent roll on page 6 and T-12 on page 7 of an offering
  memorandum) now extracts fully: 16 units and 17 T-12 lines, from the split pages and from the
  whole brochure alike. The gate then finds real problems in the broker's own figures (see
  "The gate is the safety net" below).
- `deal_b` (a small residential package) has no unit-level rent roll at all. See "When the documents are
  incomplete".

**Owner's manual baseline.** The time to key a deal into Excel by hand has not been measured yet.
`manual_entry_minutes` and `review_minutes` in each real deal's `truth_manual.yaml` are still
empty, so this README makes no time-saved claim.

## How it works

```text
rent_roll + t12 (+ proforma.yaml)
   |
   v
 pre-flight ---> plain-English banner if a file is damaged or a table is missing
   |
   v
 routing  ---> finds the rent roll / T-12 pages inside a brochure or on a shared page
   |
   v
 table readers (text layer -> wrapped cells rejoined -> model reads headers -> vision)
   |
   v
 typed records, each value with a Citation (file, page, box or cell, printed text)
   |
   v
 gate:  FOOTING -> (REEXTRACT) -> CHECKS -> REVIEW | READY      <- pure Python, no I/O
   |
   v
 Streamlit review  ->  Excel workbook with live formulas
```

Design rules that the tests enforce:

1. The model never produces a number in a typed record.
2. Money is `Decimal`, never `float`. Quantized to cents with `to_cents`.
3. Every extracted value carries a `Citation`.
4. `reconcile/` makes no model calls and does no I/O. Checks are pure functions.
5. Thresholds live only in `config/thresholds.yaml`. They are starting placeholders, to be
   calibrated on real deals.
6. The held-out deals are never listed, opened or run except by `run_eval --split heldout --final`.

## Reading real-world files

Real documents are rarely tidy. The readers try the cheap, deterministic option first and only
pay for the model when they have to (`SPEC.md` section 7.9).

- **Wrapped cells heal themselves.** Narrow columns split text across lines, sometimes inside a
  word: `Uni` over `t`, `04/03/2` over `6`, `$110,95` over `6`. The reader merges these stacks
  back into `Unit`, `04/03/26` and `$110,956`, and the citation box covers both fragments. This is
  only tried after a plain read fails, so a table that already parses is read exactly as before.
- **A missing label header is inferred.** If a T-12 prints months but no header over the line-item
  column, the column is identified from its content. Month names without a year take the year from
  the title above the table.
- **Brochures are routed, not rejected.** For a file with unrelated pages (cover, photos, pro
  forma) the router finds the rent roll page and the T-12 page, and a page that holds both
  tables is split into two regions. A pro forma page that merely mentions the rent roll is not
  mistaken for it. Only if no rent roll or T-12 can be found is the file blocked. The same file may
  go in both slots.
- **Vision is the last resort.** Order for a rent roll: text layer, text layer with wrapped cells
  rejoined, the same two with the model reading headers, then page images. A T-12 gets text
  layer, rejoined text layer, page images. Every attempt that fails leaves a note in the
  package's extraction warnings, so a reviewer can see how the file was read. Page images are
  sent only for routed pages or for short files, never page by page for a whole brochure.
  All model calls go through one client with a cost ledger, a budget cap and a replay cache.

## The gate is the safety net

The model is allowed to read, but nothing it reads is trusted. A reconciliation gate of 15
deterministic checks decides whether a deal can be exported:

`RR_FOOTING`, `RR_DUP_UNIT`, `RR_ZERO_RENT_OCC`, `RR_EXPIRED_LEASE`, `RR_CONCESSION`,
`RR_RENT_OUTLIER`, `X_RR_VS_T3`, `T12_FOOTING`, `T12_SPIKE`, `T12_NONRECURRING`, `T12_UNMAPPED`,
`PF_TAX_DROP`, `PF_INS_DROP`, `PF_VACANCY`, `PF_MGMT_FEE`.

A concrete case. In a live test of the page-image T-12 reader on `deal_a`, the model read 16 of
17 line items exactly and dropped one cell: "Court / Eviction" came back with one `$108` month
instead of two, but with the printed total of `$216`. Nothing in the model's output looks wrong.
`T12_FOOTING` adds the twelve months, finds `108` against a printed `216`, and raises a block
finding that stops the export until a person looks. The gate does not need to know what the
model got wrong. It only needs the document to disagree with itself.

Rent roll footing works the same way: the unit rows are summed and compared with the printed
total, and a failing deal triggers an independent re-read to tell an extraction error from a
document error.

What this does and does not do:

- It catches any extraction error that breaks an arithmetic relation the document itself states:
  a printed total, a subtotal, a month that sums to a year.
- It cannot catch an error where the document states no total to check against, or where two
  errors cancel. The rent roll of `deal_a` prints no total, and the gate says so
  ("footing not verifiable") rather than passing it.
- On the held-out set it also missed two planted traps, listed above, so the gate is a strong net
  and not a complete one.

Findings on a real document are the product. For `deal_a` the gate reports that the broker's
printed management total (`$6,967`) is `$2` below its own months (`$6,969`), that the printed
Total Income and Total Expenses rows do not foot, that nine units are month-to-month, and that the
rent roll runs 8.8% below the T-12's last three months. "Deposit Surrender" is left unmapped on
purpose: it could be recurring, and guessing would inflate NOI.

**Fixing a failure fixes the data.** Overriding a rent roll footing failure with a note is
refused by the engine. The reviewer must pick which number governs: use the calculated sum
(replacing the printed total), or correct a unit so the rows sum to the stated total. Either way
the corrected value is injected into the exported workbook and cited as a manual override.

## When the documents are incomplete

Early marketing packages often carry an operating statement and no unit-by-unit rent roll.
`deal_b` is exactly that: its folder held a profit and loss statement (May 2024 to April 2025),
supplied once as the "rent roll" and once as the "T-12". There is no unit-level data anywhere in
it, so the rent roll cross-audit (rent roll against T-12, footing, expired leases, outliers)
cannot run, and any tool that pretended otherwise would be inventing a rent roll.

The pre-flight step in the app stops it before the pipeline starts and says why, in plain English:

> **This file is a profit and loss statement, not a rent roll** (`rent_roll.pdf`)
> It has income and expense lines by month but no unit rows or tenant columns.
> 1. Find the unit-by-unit rent roll in the package (one row per unit).
> 2. Upload it as the rent roll. The profit and loss belongs in the T-12 slot.

The same file in the T-12 slot is accepted, since it is a valid T-12. Pre-flight also explains
damaged PDFs (the cause is named, with steps to re-export), blank pages, and mirrored text, and
announces which pages it picked out of a brochure. It blocks only when a required table is
actually absent.

## Setup and run

Requires Python 3.11 or 3.12 and [uv](https://docs.astral.sh/uv/). Commands are PowerShell.

```powershell
uv sync                                          # install
uv run ruff check .                              # lint
uv run pytest -q                                 # all tests (offline; no API key needed)
```

Run on a deal folder that holds `rent_roll.pdf` or `.xlsx`, `t12.pdf` or `.xlsx`, and an optional
`proforma.yaml`:

```powershell
uv run python -m cre_screen extract-rr data/synthetic/deal_003
uv run python -m cre_screen extract-t12 data/synthetic/deal_003
uv run python -m cre_screen run data/synthetic/deal_003 --export out/deal_003.xlsx
uv run python -m cre_screen budget               # lifetime spend from the cost ledger
uv run python -m cre_screen app                  # the review UI (Streamlit)
```

Live model calls need an `ANTHROPIC_API_KEY` in the environment or in a gitignored `.env`.
Model calls are cached under `evals/llm_cache`; the default mode is `replay`, so tests and
re-runs cost nothing:

```powershell
$env:CRE_LLM_CACHE_MODE = "replay"               # default: only cached answers, never the network
$env:CRE_LLM_CACHE_MODE = "record"               # call the model and save answers (costs money)
```

Reproduce the benchmark:

```powershell
uv run python -m synth.generate --split dev --deals 1-14 --seed 7 --out data/synthetic
uv run python -m evals.run_eval --split dev      # replay: 14 deals, $0
```

The held-out split runs once (`--split heldout --final`) and refuses to run again once a
`FINAL_*` report exists.

## Repository layout

```text
src/cre_screen/
  ingest/       preflight, routing, wrapped-cell repair, PDF and Excel table readers
  mapping/      header mapping, COA mapping, concession parsing, vision, LLM client + cache + cost
  extract/      raw tables -> typed rent roll, T-12 and pro forma records
  reconcile/    the gate and its 15 checks (pure functions)
  export/       Excel workbook with live formulas, and the Python mirror used to test it
  app/          Streamlit review UI
synth/          generator for the planted-error benchmark (dev and locked held-out deals)
evals/          scoring, reports, regression test, real-deal harness
config/         chart of accounts, header synonyms, thresholds, model prices
```

## Known limits

- The benchmark is synthetic. Real documents have only been run on two packages, unscored.
- Two planted traps were missed and two false positives were raised on held-out deal 016. Their
  cause has not been examined, by design.
- The T-12 page-image reader is a fallback. It was checked live on one page (16 of 17 line items
  exact, one dropped cell caught by the gate) and is not part of the benchmark.
- The threshold values are placeholders until calibrated on real deals.
- The broker pro forma is typed by hand into `proforma.yaml`.

## License

AGPL-3.0, because PyMuPDF (used for page rendering) is AGPL.
