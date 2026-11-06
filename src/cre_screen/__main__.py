"""CLI entry point: ``python -m cre_screen <command>``. Only CLI code prints."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from cre_screen.config import DEFAULT_CONFIG_DIR, AppConfig, load_config
from cre_screen.errors import BudgetExceededError, CacheMissError, CreScreenError
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.extract.t12 import build_t12
from cre_screen.ingest.excel_tables import extract_excel_table
from cre_screen.ingest.parse import parse_date
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table, extract_pdf_table
from cre_screen.ingest.raw import RawTable
from cre_screen.ingest.router import detect_format
from cre_screen.mapping.cache import LLMCache
from cre_screen.mapping.coa_mapper import load_learned
from cre_screen.mapping.cost import CostTracker
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.mapping.llm_client import AnthropicLLMClient
from cre_screen.reconcile.checks import rr_footing, t12_footing
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.rent_roll import RentRoll

EXIT_OK, EXIT_BAD_INPUT, EXIT_BUDGET, EXIT_CACHE_MISS = 0, 2, 3, 4
REPO_ROOT = DEFAULT_CONFIG_DIR.parent
CACHE_DIR = Path("out") / "cache" / "extract"
LEDGER = REPO_ROOT / ".cost_ledger.jsonl"
LEARNED = DEFAULT_CONFIG_DIR / "coa_learned.yaml"


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from a gitignored .env without overriding the shell."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _make_llm(cfg: AppConfig) -> tuple[AnthropicLLMClient, CostTracker, LLMCache]:
    _load_dotenv(REPO_ROOT / ".env")
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    cache_dir = cfg.models.cache.dir
    if not cache_dir.is_absolute():
        cache_dir = REPO_ROOT / cache_dir
    cache = LLMCache(cache_dir, mode=cfg.models.cache.mode, version=cfg.models.cache.version)
    tracker = CostTracker(cfg.models, LEDGER, run_id=run_id)
    return AnthropicLLMClient(cfg.models, cache, tracker), tracker, cache


def _find_input(deal_dir: Path, stem: str) -> Path:
    found = [p for p in (deal_dir / f"{stem}.pdf", deal_dir / f"{stem}.xlsx") if p.exists()]
    if len(found) != 1:
        raise CreScreenError(f"{deal_dir.as_posix()} needs exactly one {stem}.pdf or {stem}.xlsx")
    return found[0]


def _read_rr_table(path: Path, cfg: AppConfig, sheet: str | None) -> RawTable:
    fmt = detect_format(path)
    if fmt == "pdf_scanned":
        raise CreScreenError(f"{path.name} is a scanned PDF; the vision path is not built yet")
    if fmt == "xlsx":
        return extract_excel_table(path, cfg.rr_headers, sheet=sheet)
    return extract_pdf_table(path, cfg.rr_headers, cache_dir=CACHE_DIR)


def _sample_rows(table: RawTable) -> list[dict[str, str]]:
    units = [r for r in table.rows if r.kind == "unit"][:3]
    return [{h: c.text for h, c in r.cells.items()} for r in units]


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _resolve_as_of(rent_roll: RentRoll, override: str | None) -> date:
    if override:
        parsed = parse_date(override)
        if parsed is None:
            raise CreScreenError(f"--as-of is empty: {override!r}")
        return parsed
    if rent_roll.as_of_date is None:
        raise CreScreenError("no as-of date found in the document; pass --as-of YYYY-MM-DD")
    return rent_roll.as_of_date


def cmd_extract_rr(args: argparse.Namespace) -> int:
    cfg = load_config()
    llm, tracker, _cache = _make_llm(cfg)
    deal_dir: Path = args.deal_dir
    path = _find_input(deal_dir, "rent_roll")
    table = _read_rr_table(path, cfg, args.sheet)
    header_map, header_warnings = map_headers(
        table.headers, _sample_rows(table), cfg.rr_headers, llm
    )
    rent_roll = build_rent_roll(table, header_map, cfg, llm=llm)
    as_of = _resolve_as_of(rent_roll, args.as_of)
    rent_roll = rent_roll.model_copy(update={"as_of_date": as_of})

    ctx = CheckContext(
        rent_roll=rent_roll, t12=None, proforma=None, thresholds=cfg.thresholds, as_of=as_of
    )
    findings = rr_footing.check(ctx)

    print(f"{deal_dir.name}: {path.name} ({rent_roll.source_format}, {table.page_count} page(s))")
    print(f"Property: {rent_roll.property_name}   As of: {as_of.isoformat()}")
    print(f"Header map: {header_map.source}")
    print(f"Units found: {len(rent_roll.units)}")
    occupied_like = set(cfg.thresholds.rr_zero_rent_occ.occupied_like_statuses)
    for column in ("in_place_rent", "market_rent"):
        stated = rent_roll.stated_totals.get(column)  # type: ignore[call-overload]
        everyone = sum((getattr(u, column) or Decimal(0) for u in rent_roll.units), Decimal(0))
        occupied = sum(
            (
                getattr(u, column) or Decimal(0)
                for u in rent_roll.units
                if u.status in occupied_like
            ),
            Decimal(0),
        )
        stated_text = "none stated" if stated is None else _money(stated)
        print(
            f"{column}: stated {stated_text} | computed all units {_money(everyone)} | "
            f"computed occupied-like {_money(occupied)}"
        )
    for warning in [*header_warnings, *rent_roll.extraction_warnings]:
        print(f"warning: {warning}")
    failing = [f for f in findings if f.severity in ("block", "warn")]
    if failing:
        print("Footing: FAIL")
    elif findings:
        print("Footing: NOT VERIFIABLE")
    else:
        print("Footing: PASS")
    for finding in findings:
        print(f"  [{finding.severity}] {finding.message}")
    print(f"Cost this run: {_money(tracker.run_total())}")
    return EXIT_OK


def cmd_extract_t12(args: argparse.Namespace) -> int:
    cfg = load_config()
    llm, tracker, cache = _make_llm(cfg)
    deal_dir: Path = args.deal_dir
    path = _find_input(deal_dir, "t12")
    fmt = detect_format(path)
    if fmt != "pdf_text":
        raise CreScreenError(f"{path.name}: only text-layer PDF T-12s are supported so far ({fmt})")
    table = extract_pdf_t12_table(path, cfg.coa, cache_dir=CACHE_DIR)
    t12 = build_t12(table, cfg, llm=llm, learned=load_learned(LEARNED))

    print(f"{deal_dir.name}: {path.name} ({t12.source_format}, {table.page_count} page(s))")
    print(f"Property: {t12.property_name}   Period ending: {t12.period_end.isoformat()}")
    print(f"Months: {t12.month_labels[0]} .. {t12.month_labels[-1]}")
    print(f"Lines: {len(t12.lines)}   Stated subtotals: {len(t12.stated_subtotals)}")
    for line in t12.lines:
        extras = " nonrecurring" if line.is_nonrecurring else ""
        print(
            f"  {line.label:<28} {line.bucket:<22} {line.mapping_source:<13} "
            f"{line.mapping_confidence:.2f}{extras}"
        )
    sources = Counter(line.mapping_source for line in t12.lines)
    print("Mapping sources: " + ", ".join(f"{k}={v}" for k, v in sorted(sources.items())))
    for warning in t12.extraction_warnings:
        print(f"warning: {warning}")
    findings = t12_footing.footing_findings(t12, cfg.thresholds)
    print("T-12 footing: " + ("FAIL" if findings else "PASS"))
    for finding in findings:
        print(f"  [{finding.severity}] {finding.message}")
    print(f"Cost this run: {_money(tracker.run_total())} (cache mode: {cache.mode})")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cre_screen")
    sub = parser.add_subparsers(dest="command", required=True)
    rr = sub.add_parser("extract-rr", help="extract the rent roll and check that it foots")
    rr.add_argument("deal_dir", type=Path)
    rr.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD")
    rr.add_argument("--sheet", default=None)
    rr.set_defaults(func=cmd_extract_rr)
    t12 = sub.add_parser("extract-t12", help="extract the T-12 and map its lines to the COA")
    t12.add_argument("deal_dir", type=Path)
    t12.add_argument("--sheet", default=None)
    t12.set_defaults(func=cmd_extract_t12)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CacheMissError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CACHE_MISS
    except BudgetExceededError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except CreScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
