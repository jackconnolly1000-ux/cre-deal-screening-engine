"""CLI: ``python -m synth.generate --split dev --deals 1-5 --seed 7 --out data/synthetic``."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import yaml

from synth.layouts import rr_classic, t12_grid
from synth.params import load_params
from synth.traps import DEAL_PLAN
from synth.truth import DealBuild, build_deal
from synth.util import write_atomic

logger = logging.getLogger(__name__)

DEV_DEALS = range(1, 15)


def parse_range(text: str) -> list[int]:
    lo, _, hi = text.partition("-")
    first = int(lo)
    last = int(hi) if hi else first
    if first > last:
        raise argparse.ArgumentTypeError(f"bad deal range {text!r}")
    return list(range(first, last + 1))


def write_deal(build: DealBuild, out_dir: Path) -> Path:
    deal_dir = out_dir / build.truth.deal_id
    deal_dir.mkdir(parents=True, exist_ok=True)
    truth = build.truth
    rr_classic.render(
        deal_dir / "rent_roll.pdf",
        units=truth.units,
        stated_totals=truth.rr_stated_totals,
        as_of=truth.as_of_date,
        style=build.style,
    )
    t12_grid.render(
        deal_dir / "t12.pdf",
        lines=truth.t12_lines,
        month_labels=build.month_labels,
        period_end=truth.as_of_date,
        style=build.style,
    )
    proforma_yaml = yaml.safe_dump(build.proforma, sort_keys=False, allow_unicode=True)
    write_atomic(deal_dir / "proforma.yaml", proforma_yaml.encode("utf-8"))
    write_atomic(deal_dir / "truth.json", (truth.model_dump_json(indent=2) + "\n").encode("utf-8"))
    meta_json = json.dumps(build.meta, indent=2, sort_keys=True) + "\n"
    write_atomic(deal_dir / "meta.json", meta_json.encode("utf-8"))
    return deal_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.generate")
    parser.add_argument("--split", choices=["dev", "heldout"], required=True)
    parser.add_argument("--deals", type=parse_range, required=True, help="e.g. 1-5")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overwrite-dev", action="store_true")
    parser.add_argument("--force-heldout", action="store_true")
    args = parser.parse_args(argv)

    if args.split == "heldout":
        print("error: held-out generation arrives in M6; nothing was written.", file=sys.stderr)
        return 2
    bad = [n for n in args.deals if n not in DEV_DEALS or n not in DEAL_PLAN]
    if bad:
        print(f"error: dev deals are 1-14 (got {bad})", file=sys.stderr)
        return 2
    existing = [n for n in args.deals if (args.out / f"deal_{n:03d}").exists()]
    if existing and not args.overwrite_dev:
        print(
            f"error: deal folders already exist for {existing}; pass --overwrite-dev",
            file=sys.stderr,
        )
        return 2

    params = load_params()
    for n in args.deals:
        deal_dir = args.out / f"deal_{n:03d}"
        if deal_dir.exists():
            shutil.rmtree(deal_dir)
        build = build_deal(n, args.seed, params, split=args.split)
        write_deal(build, args.out)
        traps = ", ".join(build.truth.traps) or "clean control"
        print(f"{build.truth.deal_id}: {len(build.truth.units)} rows, {traps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
