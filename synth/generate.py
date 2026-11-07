"""CLI: ``python -m synth.generate --split dev --deals 1-5 --seed 7 --out data/synthetic``.

Held-out deals (015 to 020) are written once, together with ``HELDOUT_MANIFEST.sha256``. After
that they are locked: the generator refuses to touch them without ``--force-heldout``, and this
module never prints their contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from synth.degrade import degrade_pdf
from synth.layouts import RR, T12
from synth.params import load_params
from synth.plan import DEV_DEALS, HELDOUT_DEALS
from synth.truth import DealBuild, build_deal
from synth.util import write_atomic

logger = logging.getLogger(__name__)

MANIFEST_NAME = "HELDOUT_MANIFEST.sha256"
HELDOUT_SEED = 1015


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
    truth, plan = build.truth, build.plan

    rr_path = deal_dir / ("rent_roll.xlsx" if plan.rr_layout == "rr_excel" else "rent_roll.pdf")
    rr_args = {
        "units": truth.units,
        "stated_totals": truth.rr_stated_totals,
        "as_of": truth.as_of_date,
        "style": build.style,
        "footnotes": build.footnotes,
    }
    if plan.scanned:
        with tempfile.TemporaryDirectory() as scratch:
            clean = Path(scratch) / "rent_roll.pdf"
            RR[plan.rr_layout].render(clean, **rr_args)
            degrade_pdf(clean, rr_path, seed=int(build.meta["deal_seed"]))  # type: ignore[arg-type]
    else:
        RR[plan.rr_layout].render(rr_path, **rr_args)

    t12_path = deal_dir / ("t12.xlsx" if plan.t12_layout == "t12_excel" else "t12.pdf")
    T12[plan.t12_layout].render(
        t12_path,
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


def heldout_manifest(out_dir: Path) -> str:
    """``sha256  relative/path`` for every file in the held-out deal folders, sorted by path."""
    lines = []
    for number in HELDOUT_DEALS:
        folder = out_dir / f"deal_{number:03d}"
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(out_dir).as_posix()}")
    return "\n".join(lines) + "\n"


def verify_manifest(out_dir: Path) -> list[str]:
    """Problems found comparing the held-out folders with the manifest; empty means locked."""
    manifest = out_dir / MANIFEST_NAME
    if not manifest.exists():
        return [f"{MANIFEST_NAME} is missing"]
    listed: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, _, relative = line.partition("  ")
        listed[relative] = digest
    present = {
        path.relative_to(out_dir).as_posix(): path
        for number in HELDOUT_DEALS
        for path in (out_dir / f"deal_{number:03d}").rglob("*")
        if path.is_file()
    }
    problems = [f"missing file: {name}" for name in sorted(set(listed) - set(present))]
    problems += [f"unlisted file: {name}" for name in sorted(set(present) - set(listed))]
    for name in sorted(set(listed) & set(present)):
        if hashlib.sha256(present[name].read_bytes()).hexdigest() != listed[name]:
            problems.append(f"changed file: {name}")
    return problems


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m synth.generate")
    parser.add_argument("--split", choices=["dev", "heldout"], required=True)
    parser.add_argument("--deals", type=parse_range, required=True, help="e.g. 1-5")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overwrite-dev", action="store_true")
    parser.add_argument("--force-heldout", action="store_true")
    args = parser.parse_args(argv)

    if args.split == "dev":
        bad = [n for n in args.deals if n not in DEV_DEALS]
        if bad:
            return _fail(f"dev deals are 1-14 (got {bad})")
        existing = [n for n in args.deals if (args.out / f"deal_{n:03d}").exists()]
        if existing and not args.overwrite_dev:
            return _fail(f"deal folders already exist for {existing}; pass --overwrite-dev")
    else:
        if args.deals != list(HELDOUT_DEALS):
            return _fail("held-out generation writes deals 15-20 together, with their manifest")
        manifest = args.out / MANIFEST_NAME
        locked = [n for n in HELDOUT_DEALS if (args.out / f"deal_{n:03d}").exists()]
        if (locked or manifest.exists()) and not args.force_heldout:
            return _fail("held-out deals are locked; refusing to overwrite without --force-heldout")

    params = load_params()
    for n in args.deals:
        deal_dir = args.out / f"deal_{n:03d}"
        if deal_dir.exists():
            shutil.rmtree(deal_dir)
        build = build_deal(n, args.seed, params, split=args.split)
        write_deal(build, args.out)
        if args.split == "dev":
            traps = ", ".join(build.truth.traps) or "clean control"
            print(f"{build.truth.deal_id}: {len(build.truth.units)} rows, {traps}")
        else:
            print(f"{build.truth.deal_id}: written")

    if args.split == "heldout":
        write_atomic(args.out / MANIFEST_NAME, heldout_manifest(args.out).encode("utf-8"))
        print(f"{MANIFEST_NAME}: written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
