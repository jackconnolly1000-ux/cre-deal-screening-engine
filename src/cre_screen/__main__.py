"""CLI entry point: ``python -m cre_screen <command>``. Only CLI code prints."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from cre_screen.config import AppConfig, load_config
from cre_screen.errors import BudgetExceededError, CacheMissError, CreScreenError
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.ingest.excel_tables import extract_excel_table
from cre_screen.ingest.parse import parse_date
from cre_screen.ingest.pdf_tables import extract_pdf_table
from cre_screen.ingest.raw import RawTable
from cre_screen.ingest.router import detect_format
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.mapping.llm_protocol import NullLLMClient
from cre_screen.reconcile.checks import rr_footing
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.rent_roll import RentRoll

EXIT_OK, EXIT_BAD_INPUT, EXIT_BUDGET, EXIT_CACHE_MISS = 0, 2, 3, 4
CACHE_DIR = Path("out") / "cache" / "extract"


def _find_input(deal_dir: Path, stem: str) -> Path:
    found = [p for p in (deal_dir / f"{stem}.pdf", deal_dir / f"{stem}.xlsx") if p.exists()]
    if len(found) != 1:
        raise CreScreenError(f"{deal_dir.as_posix()} needs exactly one {stem}.pdf or {stem}.xlsx")
    return found[0]


def _read_table(path: Path, cfg: AppConfig, sheet: str | None) -> RawTable:
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


def cmd_extract_rr(args: argparse.Namespace) -> int:
    cfg = load_config()
    deal_dir: Path = args.deal_dir
    path = _find_input(deal_dir, "rent_roll")
    table = _read_table(path, cfg, args.sheet)
    header_map, header_warnings = map_headers(
        table.headers, _sample_rows(table), cfg.rr_headers, NullLLMClient()
    )
    rent_roll = build_rent_roll(table, header_map, cfg, llm=NullLLMClient())
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
        attr = column
        everyone = sum((getattr(u, attr) or Decimal(0) for u in rent_roll.units), Decimal(0))
        occupied = sum(
            (getattr(u, attr) or Decimal(0) for u in rent_roll.units if u.status in occupied_like),
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
    return EXIT_OK


def _resolve_as_of(rent_roll: RentRoll, override: str | None) -> date:
    if override:
        parsed = parse_date(override)
        if parsed is None:
            raise CreScreenError(f"--as-of is empty: {override!r}")
        return parsed
    if rent_roll.as_of_date is None:
        raise CreScreenError("no as-of date found in the document; pass --as-of YYYY-MM-DD")
    return rent_roll.as_of_date


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cre_screen")
    sub = parser.add_subparsers(dest="command", required=True)
    rr = sub.add_parser("extract-rr", help="extract the rent roll and check that it foots")
    rr.add_argument("deal_dir", type=Path)
    rr.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD")
    rr.add_argument("--sheet", default=None)
    rr.set_defaults(func=cmd_extract_rr)
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
