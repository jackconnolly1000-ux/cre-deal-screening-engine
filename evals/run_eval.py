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
REPORTS_DIR = REPO_ROOT / "evals" / "reports"
BASELINE_PATH = REPO_ROOT / "evals" / "baseline_dev.json"
EXIT_OK, EXIT_BAD_INPUT, EXIT_BUDGET, EXIT_CACHE_MISS = 0, 2, 3, 4


def evaluate(
    cfg: AppConfig, *, out_dir: Path | None = None, data_dir: Path = DATA_DIR
) -> tuple[list[DealScore], Decimal]:
    """Run the pipeline on every dev deal and score it. Returns (scores, total cost)."""
    llm, tracker, _cache = make_llm(cfg)
    out = out_dir or REPO_ROOT / "out"
    scores: list[DealScore] = []
    for number in DEV_DEAL_NUMBERS:
        deal_dir = data_dir / f"deal_{number:03d}"
        truth = DealTruth.model_validate_json((deal_dir / "truth.json").read_bytes())
        spent_before = tracker.run_total()
        try:
            pkg = run_deal(
                deal_dir, llm=llm, cfg=cfg, tracker=tracker, out_dir=out, learned_path=LEARNED
            )
        except (CacheMissError, BudgetExceededError, ConfigError):
            raise
        except CreScreenError as exc:
            logger.error("%s failed: %s", deal_dir.name, exc)
            scores.append(failed_score(truth, str(exc)))
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
    parser.add_argument("--out", type=Path, default=REPORTS_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.split != "dev":
        print(
            f"error: --split {args.split} is not available yet (held-out and real runs belong "
            "to the final milestones)",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    if args.final:
        print("error: --final applies to the held-out split only", file=sys.stderr)
        return EXIT_BAD_INPUT

    cfg = with_overrides(
        load_config(), cache_mode=args.cache_mode, model=args.model, run_cap=args.run_cap
    )
    try:
        scores, cost = evaluate(cfg)
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
