# CLAUDE.md

Standing instructions for Claude Code in the `cre-screen` repo. Read this file and the relevant sections of `SPEC.md` at the start of every session.

## What this repo is

A multifamily deal-screening engine: extract rent rolls and T-12s with citations, map T-12 lines to a fixed chart of accounts, run a deterministic reconciliation gate, export a live-formula Excel workbook, and measure all of it on a planted-error benchmark. `SPEC.md` is the contract. This file is how to work.

## Current milestone

```text
CURRENT: M0
DONE:    (none)
```

The owner updates this block. Work only on the CURRENT milestone. If a task would touch a later milestone's files, stop and ask.

## Session workflow

1. Read the CURRENT milestone in `SPEC.md` Section 12, plus every section it references.
2. Write a short plan in chat: files you will create or change, tests you will write, anything ambiguous. Wait for "go" if anything is ambiguous; otherwise proceed.
3. Write tests first for checks, parsers and scoring. Then implement.
4. Run the milestone's acceptance commands exactly as written in `SPEC.md`.
5. Stop. Report in this format:

```text
Milestone: M_
Changed: <files>
Acceptance: <each command> -> pass/fail
Spend this session: $<from `uv run python -m cre_screen budget`, or "no live calls">
Open questions: <list or "none">
```

6. Commit only after acceptance passes, one commit per logical step, message `M<n>: <what>`. Do not push. The owner pushes.

## Commands (PowerShell)

```powershell
uv sync                                         # install
uv run ruff check .                             # lint
uv run ruff format .                            # format
uv run pytest -q                                # all tests except live and heldout
uv run pytest tests/test_<name>.py -q           # one file
uv run python -m synth.generate --split dev --deals 1-14 --seed 7 --out data/synthetic
uv run python -m cre_screen extract-rr data/synthetic/deal_003
uv run python -m cre_screen extract-t12 data/synthetic/deal_003
uv run python -m cre_screen run data/synthetic/deal_003 --export out/deal_003.xlsx
uv run python -m cre_screen budget
uv run python -m evals.run_eval --split dev
uv run streamlit run src/cre_screen/app/streamlit_app.py
$env:CRE_LLM_CACHE_MODE = "replay"              # default; "record" only when the owner approves
```

## Hard rules

Breaking any of these is a bug even if tests pass.

1. **The LLM never produces a number that lands in a typed record.** It maps headers and labels, parses concession notes (every number grounded in the note's text), and transcribes scanned pages as strings that code parses. Nothing else. See SPEC Section 2.
2. **Money is `Decimal`, quantized with `to_cents`.** No `float` for money in `src/`, `synth/`, `evals/`. Never `Decimal(some_float)`; use `Decimal(str(x))` for numbers that arrive as floats (openpyxl returns floats for numeric cells).
3. **Every extracted value carries a `Citation`.**
4. **`reconcile/` makes no LLM calls and has no I/O.** Checks are pure functions of `CheckContext`.
5. **Thresholds live only in `config/thresholds.yaml`.** Never change a threshold to make a test pass. If a threshold looks wrong, stop and ask.
6. **Schemas enforce shape, checks enforce rules.** Never add a validator that rejects a suspicious-but-real value ($0 rent, expired lease, duplicate unit).
7. **Copy the schemas in SPEC Section 6 exactly.** Adding a field, changing a type, or loosening a constraint needs the owner's OK and a SPEC edit by the owner.
8. **No live API calls without asking.** Before any `record` run, state the command, the number of calls, and an estimated cost from `config/models.yaml` prices, then wait for "go". Tests never call the API; use `tests/fakes.py::FakeLLMClient`.
9. **Only `mapping/llm_client.py` imports `anthropic` or `instructor`. Only `ingest/render.py` imports `fitz`.**
10. **Held-out deals (015 to 020) are off limits.** Do not open, read, print, or run the pipeline on them. Do not read `data/synthetic/HELDOUT_MANIFEST.sha256` except in `tests/test_heldout_lock.py`. The only exception is the owner's M8 command `run_eval --split heldout --final`.
11. **Never weaken a test to make it pass.** No `skip`, `xfail`, loosened tolerance, or deleted assertion without the owner's OK.
12. **Excel formulas use bounded ranges only** (`$Q$2:$Q$201`), never whole columns (`$Q:$Q`). The `formulas` package in CI must evaluate every formula.
13. **No new dependencies** beyond SPEC Section 3.1 without asking.
14. **Never commit** `data/real/`, `.env`, `out/`, or anything containing an API key.
15. **No agent frameworks** (LangGraph, CrewAI, LlamaIndex) and no vector databases.

## Windows rules

- `pathlib.Path` for every path. Never build paths with string concatenation or `os.sep`.
- `encoding="utf-8"` on every `open`, `read_text`, `write_text`. `newline=""` for CSV.
- Store paths inside JSON and citations with `/` separators (`Path.as_posix()`).
- No shell scripts, no `os.system`, no `subprocess` calls to POSIX tools, no symlinks.
- Atomic file writes: write to `name.tmp`, then `Path.replace`.
- Commands in docs and reports are PowerShell-compatible.

## Code conventions

- Python 3.11+ syntax, full type hints, `from __future__ import annotations` at the top of every module.
- Ruff settings in `pyproject.toml`. Run `uv run ruff format .` before every commit.
- One check per file in `reconcile/checks/`, named after its check ID in lowercase. Each file exports one function `check(ctx: CheckContext) -> list[Finding]`.
- Custom exceptions live in `src/cre_screen/errors.py` and inherit from `CreScreenError`.
- Library code logs with `logging.getLogger(__name__)`. Only CLI code prints.
- Test names describe the case: `test_footing_fails_one_cent_over_tolerance`.
- Every new check ships with a matching trap in `synth/traps.py` and a test file with a pass case, a fail case, the exact threshold edge, and missing-input behavior.

## Known gotchas

- `openpyxl` with `data_only=True` returns `None` for formula cells in files that Excel never opened. The generator must write **values**, not formulas, into synthetic input workbooks.
- `formulas` returns NumPy arrays and NumPy floats. Convert with `Decimal(str(float(v)))` before comparing to `uw_math` results.
- pdfplumber coordinates use `top` measured from the page top, in PDF points. Keep all bbox math in that convention; `render.draw_boxes` converts to pixels with `scale = dpi / 72`.
- Excel stores rents as floats. `Decimal(str(1250.1))` is `1250.1`; then `to_cents`.
- Set `reportlab.rl_config.invariant = 1` before rendering so PDFs are reproducible. If the installed ReportLab ignores it, compare extracted text in tests, not file bytes, and tell the owner.
- Instructor's client API and hook names change between versions. Check the installed version's docs before writing `llm_client.py`, and tell the owner if the SPEC's call shape is out of date.
- Haiku 5.5 input price rises 5x above 100,000 prompt tokens. The size guard in `llm_client.py` must run before every live call.
- `streamlit.testing.v1.AppTest` runs the script top to bottom; keep pipeline calls behind the Run button and `st.cache_data`.

## Stop and ask when

- The SPEC is silent or contradicts itself.
- A test fails and the fix would change a schema, threshold, formula layout, or trap definition.
- A live API call is needed.
- Anything requires touching held-out deals.
- A dependency is missing or behaves differently than the SPEC says.
- The work spills into another milestone.
