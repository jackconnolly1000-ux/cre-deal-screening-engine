"""CLI: ``python -m evals.run_eval --split dev``.

Only the dev split is available. Held-out deals (015 to 020) are never listed, opened or run by
this module: the dev deal numbers are spelled out, not discovered by globbing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from cre_screen.config import AppConfig, load_config
from cre_screen.errors import (
    BudgetExceededError,
    CacheMissError,
    ConfigError,
    CreScreenError,
)
from cre_screen.pipeline import run_deal
from cre_screen.runtime import LEARNED, REPO_ROOT, make_llm, with_overrides
from cre_screen.schemas.deal import DealScore, DealTruth
from evals.report import aggregate, rate, write_reports
from evals.scoring import failed_score, score_deal

logger = logging.getLogger(__name__)

DEV_DEAL_NUMBERS = range(1, 15)
DATA_DIR = REPO_ROOT / "data" / "synthetic"
REAL_DIR = REPO_ROOT / "data" / "real"
REPORTS_DIR = REPO_ROOT / "evals" / "reports"
BASELINE_PATH = REPO_ROOT / "evals" / "baseline_dev.json"
EXIT_OK, EXIT_BAD_INPUT, EXIT_BUDGET, EXIT_CACHE_MISS = 0, 2, 3, 4


NO_AS_OF = "no as-of date found"


def evaluate(
    cfg: AppConfig,
    *,
    out_dir: Path | None = None,
    data_dir: Path = DATA_DIR,
    notes: list[str] | None = None,
) -> tuple[list[DealScore], Decimal]:
    """Run the pipeline on every dev deal and score it. Returns (scores, total cost).

    When a document yields no as-of date (a scan whose title lines the model did not transcribe)
    the real CLI asks the operator for ``--as-of``. The harness plays the operator: it reruns the
    deal with the truth's date, and says so in ``notes``, so the limitation stays visible.
    """
    llm, tracker, _cache = make_llm(cfg)
    out = out_dir or REPO_ROOT / "out"
    scores: list[DealScore] = []
    for number in DEV_DEAL_NUMBERS:
        deal_dir = data_dir / f"deal_{number:03d}"
        truth = DealTruth.model_validate_json((deal_dir / "truth.json").read_bytes())
        spent_before = tracker.run_total()
        as_of = None
        while True:
            try:
                pkg = run_deal(
                    deal_dir,
                    llm=llm,
                    cfg=cfg,
                    as_of=as_of,
                    tracker=tracker,
                    out_dir=out,
                    learned_path=LEARNED,
                )
                break
            except (CacheMissError, BudgetExceededError, ConfigError):
                raise
            except CreScreenError as exc:
                if NO_AS_OF in str(exc) and as_of is None:
                    as_of = truth.as_of_date
                    if notes is not None:
                        notes.append(f"{deal_dir.name}: no as-of date read; supplied {as_of}")
                    continue
                logger.error("%s failed: %s", deal_dir.name, exc)
                scores.append(failed_score(truth, str(exc)))
                pkg = None
                break
        if pkg is None:
            continue
        pkg = pkg.model_copy(update={"cost_usd": tracker.run_total() - spent_before})
        scores.append(score_deal(pkg, truth))
    return scores, tracker.run_total()


def baseline_from(scores: list[DealScore]) -> dict[str, object]:
    overall = aggregate(scores)["overall"]
    return {
        "split": "dev",
        "deals": overall["deals"],
        "planted": overall["planted"],
        "caught": overall["caught"],
        "false_positives": overall["false_positives"],
        "clean_control_findings": overall["clean_control_findings"],
        "numeric_fields_total": overall["numeric_fields_total"],
        "numeric_fields_correct": overall["numeric_fields_correct"],
        "numeric_accuracy": overall["numeric_accuracy"],
        "per_deal": {s.deal_id: {"planted": s.planted, "caught": s.caught} for s in scores},
    }


def _git_sha() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "nogit"
    return done.stdout.strip() or "nogit"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.run_eval")
    parser.add_argument("--split", choices=["dev", "heldout", "real", "all"], required=True)
    parser.add_argument("--cache-mode", choices=["replay", "record", "off"], default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--run-cap", type=Decimal, default=None)
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--allow-second-final", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--update-timing", action="store_true")
    parser.add_argument("--out", type=Path, default=REPORTS_DIR)
    return parser


def _run_real(cfg: AppConfig, args: argparse.Namespace) -> int:
    """``--split real``: score the owner's hand-checked public deals in data/real."""
    from evals.real import evaluate_real, manual_timing, render_markdown, scores_json

    try:
        scores = evaluate_real(cfg, REAL_DIR, REPO_ROOT / "out")
    except CacheMissError as exc:
        print(
            f"error: {exc}\nrecord the missing entries first (CRE_LLM_CACHE_MODE=record)",
            file=sys.stderr,
        )
        return EXIT_CACHE_MISS
    except BudgetExceededError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except CreScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    sha, today = _git_sha(), f"{datetime.now(UTC):%Y-%m-%d}"
    cost = sum((s.cost_usd for s in scores), Decimal("0"))
    meta = {
        "split": "real",
        "date_utc": today,
        "git_sha": sha,
        "cache_mode": cfg.models.cache.mode,
        "model": cfg.models.default_model,
        "cost_usd": str(cost),
    }
    stem = f"{today}_{sha}_real"
    _write_atomic(args.out / f"{stem}.json", scores_json(scores))
    _write_atomic(args.out / f"{stem}.md", render_markdown(scores, meta))
    print(f"real: {len(scores)} deal(s), cost ${cost}")
    for s in scores:
        print(
            f"  {s.deal}: units {s.units_correct}/{s.units_checked}, "
            f"totals {s.totals_correct}/{s.totals_checked}, "
            f"T-12 lines {s.lines_correct}/{s.lines_checked}, "
            f"owner findings caught {len(s.caught_findings)}/{len(s.agreed_findings)}"
        )
        for miss in s.misses:
            print(f"    miss: {miss}")
    print(f"report: {(args.out / f'{stem}.md').as_posix()}")
    if args.update_timing:
        timing = REPO_ROOT / "evals" / "manual_timing.yaml"
        _write_atomic(timing, yaml.safe_dump(manual_timing(scores), sort_keys=True))
        print(f"timing pairs written: {timing.relative_to(REPO_ROOT).as_posix()}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.split not in ("dev", "real"):
        print(
            f"error: --split {args.split} is not available yet (the held-out run belongs to the "
            "final milestone)",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    if args.final:
        print("error: --final applies to the held-out split only", file=sys.stderr)
        return EXIT_BAD_INPUT

    cfg = with_overrides(
        load_config(), cache_mode=args.cache_mode, model=args.model, run_cap=args.run_cap
    )
    if args.split == "real":
        return _run_real(cfg, args)
    notes: list[str] = []
    try:
        scores, cost = evaluate(cfg, notes=notes)
    except CacheMissError as exc:
        print(
            f"error: {exc}\nrecord the missing entries first (CRE_LLM_CACHE_MODE=record)",
            file=sys.stderr,
        )
        return EXIT_CACHE_MISS
    except BudgetExceededError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BUDGET
    except CreScreenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    sha = _git_sha()
    today = f"{datetime.now(UTC):%Y-%m-%d}"
    meta = {
        "split": "dev",
        "date_utc": today,
        "git_sha": sha,
        "cache_mode": cfg.models.cache.mode,
        "model": cfg.models.default_model,
        "cost_usd": str(cost),
    }
    if notes:
        meta["as_of_supplied_by_harness"] = "; ".join(notes)
    json_path, md_path = write_reports(scores, args.out, f"{today}_{sha}_dev", meta)
    overall = aggregate(scores)["overall"]
    print(f"dev: {overall['deals']} deals, cost ${cost}")
    print(
        f"caught {overall['caught']}/{overall['planted']}, false positives "
        f"{overall['false_positives']}, clean-control findings {overall['clean_control_findings']}"
    )
    print(
        f"numeric accuracy {overall['numeric_accuracy']}, unit recall {overall['unit_recall']}, "
        f"phantoms {overall['phantom_units']}, COA $ accuracy {overall['coa_accuracy']}"
    )
    for note in notes:
        print(f"note: {note}")
    print(f"report: {md_path.as_posix()}  ({json_path.name})")
    for s in scores:
        for miss in s.misses:
            print(f"  miss {s.deal_id}: {miss}")
    if args.update_baseline:
        _write_atomic(BASELINE_PATH, json.dumps(baseline_from(scores), indent=2) + "\n")
        print(f"baseline rewritten: {BASELINE_PATH.relative_to(REPO_ROOT).as_posix()}")
    return EXIT_OK


__all__ = ["baseline_from", "evaluate", "main", "rate"]

if __name__ == "__main__":
    raise SystemExit(main())
