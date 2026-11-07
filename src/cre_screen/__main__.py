"""CLI entry point: ``python -m cre_screen <command>``. Only CLI code prints."""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from cre_screen.config import AppConfig, load_config
from cre_screen.errors import BudgetExceededError, CacheMissError, CreScreenError
from cre_screen.export.excel_writer import write_workbook
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.extract.t12 import build_t12
from cre_screen.ingest.excel_tables import extract_excel_t12_table
from cre_screen.ingest.parse import parse_date
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table
from cre_screen.ingest.router import detect_format
from cre_screen.mapping.coa_mapper import load_learned
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.pipeline import find_input, read_rr_table, run_deal, sample_rows
from cre_screen.reconcile.checks import rr_footing, t12_footing
from cre_screen.reconcile.context import CheckContext
from cre_screen.runtime import LEARNED, REPO_ROOT, make_llm, with_overrides
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.rent_roll import RentRoll

EXIT_OK, EXIT_BAD_INPUT, EXIT_BUDGET, EXIT_CACHE_MISS, EXIT_OPEN_BLOCKS = 0, 2, 3, 4, 5
CACHE_DIR = Path("out") / "cache" / "extract"
SEVERITY_ORDER = ("block", "warn", "info")


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
    llm, tracker, _cache = make_llm(cfg)
    deal_dir: Path = args.deal_dir
    path = find_input(deal_dir, "rent_roll")
    assert path is not None
    table = read_rr_table(path, cfg, args.sheet, CACHE_DIR, llm)
    header_map, header_warnings = map_headers(
        table.headers, sample_rows(table), cfg.rr_headers, llm
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
    llm, tracker, cache = make_llm(cfg)
    deal_dir: Path = args.deal_dir
    path = find_input(deal_dir, "t12")
    assert path is not None
    fmt = detect_format(path)
    if fmt == "pdf_scanned":
        raise CreScreenError(f"{path.name}: scanned T-12s are out of scope for v1")
    if fmt == "xlsx":
        table = extract_excel_t12_table(path, cfg.coa, sheet=args.sheet)
    else:
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


def cmd_run(args: argparse.Namespace) -> int:
    cfg: AppConfig = with_overrides(
        load_config(),
        model=args.model,
        run_cap=Decimal(args.run_cap) if args.run_cap else None,
    )
    llm, tracker, cache = make_llm(cfg)
    deal_dir: Path = args.deal_dir
    as_of = parse_date(args.as_of) if args.as_of else None
    pkg = run_deal(deal_dir, llm=llm, cfg=cfg, as_of=as_of, tracker=tracker, learned_path=LEARNED)

    print(f"{pkg.deal_id}: {pkg.rent_roll.source_file}, as of {pkg.rent_roll.as_of_date}")
    print(
        f"Units: {len(pkg.rent_roll.units)}   T-12 lines: "
        f"{len(pkg.t12.lines) if pkg.t12 else 'none'}   Pro forma: "
        f"{'yes' if pkg.proforma else 'none'}"
    )
    print("Gate: " + " -> ".join([pkg.gate_log[0].from_state, *(t.to_state for t in pkg.gate_log)]))
    counts = Counter(f.severity for f in pkg.findings)
    print(
        "Findings: "
        + ", ".join(f"{counts.get(s, 0)} {s}" for s in SEVERITY_ORDER)
        + f"   Open blocks: {len(pkg.open_blocks())}"
    )
    for severity in SEVERITY_ORDER:
        for finding in pkg.findings:
            if finding.severity != severity:
                continue
            cause = f" (cause: {finding.details['cause']})" if "cause" in finding.details else ""
            print(f"  [{severity}] {finding.check_id}: {finding.message}{cause}")
    for warning in pkg.rent_roll.extraction_warnings:
        print(f"warning: {warning}")
    print(f"Final state: {pkg.final_state}")
    print(f"Package: out/{pkg.deal_id}/package.json")
    print(f"Cost this run: {_money(pkg.cost_usd)} (cache mode: {cache.mode})")
    if args.export is None:
        return EXIT_OK
    return _export(
        pkg, Path(args.export), args.allow_open_blocks, (deal_dir / "truth.json").exists()
    )


def _export(pkg: DealPackage, path: Path, allow_open_blocks: bool, synthetic: bool) -> int:
    """Write the workbook unless blocks are open (exit 5), unless explicitly allowed."""
    blocks = pkg.open_blocks()
    if blocks and not allow_open_blocks:
        print(
            f"error: export refused: {len(blocks)} open block finding(s); clear them in review "
            "or pass --allow-open-blocks",
            file=sys.stderr,
        )
        return EXIT_OPEN_BLOCKS
    write_workbook(pkg, path, synthetic=synthetic)
    note = f" (with {len(blocks)} open block finding(s))" if blocks else ""
    print(f"Workbook: {path.as_posix()}{note}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    package_path: Path = args.package
    try:
        pkg = DealPackage.model_validate_json(package_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CreScreenError(f"cannot read package {package_path.as_posix()}: {exc}") from exc
    synthetic = (REPO_ROOT / "data" / "synthetic" / pkg.deal_id / "truth.json").exists()
    return _export(pkg, args.out, args.allow_open_blocks, synthetic)


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
    run = sub.add_parser(
        "run", aliases=["screen"], help="full pipeline: extract, reconcile, write the package"
    )
    run.add_argument("deal_dir", type=Path)
    run.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD")
    run.add_argument("--export", default=None, help="write the workbook here")
    run.add_argument("--allow-open-blocks", action="store_true")
    run.add_argument("--model", default=None)
    run.add_argument("--run-cap", dest="run_cap", default=None, help="USD, e.g. 1.00")
    run.set_defaults(func=cmd_run)
    export = sub.add_parser("export", help="write the workbook from a saved package.json")
    export.add_argument("package", type=Path)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--allow-open-blocks", action="store_true")
    export.set_defaults(func=cmd_export)
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
