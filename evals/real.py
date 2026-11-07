"""Real-deal sanity set (SPEC 11.5): public documents scored by hand.

``data/real/<name>/`` holds the same files as a synthetic deal except ``truth.json`` is replaced
by ``truth_manual.yaml``: the owner's spot checks, written down as printed. This module loads
that file, runs the pipeline, and reports how many spot checks the pipeline got right and which
findings the owner agrees with. ``data/real/`` is gitignored, so nothing here ever commits a
real document.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cre_screen.config import AppConfig
from cre_screen.errors import ConfigError, CreScreenError
from cre_screen.pipeline import run_deal
from cre_screen.reconcile.metrics import normalize_unit_id
from cre_screen.runtime import LEARNED, make_llm
from cre_screen.schemas.common import CheckId, COABucket, Money
from cre_screen.schemas.deal import DealPackage

TRUTH_FILE = "truth_manual.yaml"


def _money(value: Any) -> Any:
    """YAML money must be a quoted string: a bare 1250.10 would arrive as a float."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise ValueError(f"{value!r} is a YAML float; quote money values as strings")
    if isinstance(value, (str, int)):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"{value!r} is not a number") from exc
    raise ValueError(f"cannot read {value!r} as money")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpotUnit(_Strict):
    unit_id: str
    in_place_rent: Money | None = None
    market_rent: Money | None = None

    _coerce = field_validator("in_place_rent", "market_rent", mode="before")(_money)


class SpotLine(_Strict):
    label: str
    bucket: COABucket
    total: Money

    _coerce = field_validator("total", mode="before")(_money)


class ManualTruth(_Strict):
    source_url: str
    retrieved: str
    units_spot_checked: list[SpotUnit] = []
    stated_totals: dict[str, Money] = {}
    t12_lines_spot_checked: list[SpotLine] = []
    findings_owner_agrees_with: list[CheckId] = []
    manual_entry_minutes: Decimal | None = None
    review_minutes: Decimal | None = None

    @field_validator("stated_totals", mode="before")
    @classmethod
    def _totals(cls, value: Any) -> Any:
        return {k: _money(v) for k, v in (value or {}).items()}

    @field_validator("manual_entry_minutes", "review_minutes", mode="before")
    @classmethod
    def _minutes(cls, value: Any) -> Any:
        return None if value is None else Decimal(str(value))


class RealScore(_Strict):
    deal: str
    units_checked: int
    units_correct: int
    totals_checked: int
    totals_correct: int
    lines_checked: int
    lines_correct: int
    agreed_findings: list[str]
    caught_findings: list[str]
    unconfirmed_findings: list[str] = Field(description="warn/block checks the owner did not list")
    final_state: str
    cost_usd: Decimal
    wall_seconds: float
    manual_entry_minutes: Decimal | None
    review_minutes: Decimal | None
    misses: list[str]


def load_truth(path: Path) -> ManualTruth:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path.as_posix()}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.as_posix()} must contain a mapping")
    try:
        return ManualTruth.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path.as_posix()}: {exc}") from exc


def real_deal_dirs(real_dir: Path) -> list[Path]:
    """Folders under data/real that have a truth_manual.yaml, sorted by name."""
    if not real_dir.is_dir():
        return []
    return sorted(p for p in real_dir.iterdir() if (p / TRUTH_FILE).is_file())


def score_real(pkg: DealPackage, truth: ManualTruth) -> RealScore:
    misses: list[str] = []
    units = {normalize_unit_id(u.unit_id): u for u in pkg.rent_roll.units}
    units_correct = 0
    for spot in truth.units_spot_checked:
        got = units.get(normalize_unit_id(spot.unit_id))
        if got is None:
            misses.append(f"unit {spot.unit_id}: not extracted")
            continue
        wrong = [
            f"{name} {getattr(got, name)} != {getattr(spot, name)}"
            for name in ("in_place_rent", "market_rent")
            if getattr(spot, name) is not None and getattr(got, name) != getattr(spot, name)
        ]
        if wrong:
            misses.append(f"unit {spot.unit_id}: " + "; ".join(wrong))
        else:
            units_correct += 1

    totals_correct = 0
    for name, wanted in truth.stated_totals.items():
        got_total = pkg.rent_roll.stated_totals.get(name)  # type: ignore[call-overload]
        if got_total == wanted:
            totals_correct += 1
        else:
            misses.append(f"stated total {name}: read {got_total}, printed {wanted}")

    lines = {ln.label: ln for ln in (pkg.t12.lines if pkg.t12 else [])}
    lines_correct = 0
    for spot_line in truth.t12_lines_spot_checked:
        found = lines.get(spot_line.label)
        if found is None:
            misses.append(f"T-12 line '{spot_line.label}': not extracted")
        elif found.bucket != spot_line.bucket:
            misses.append(
                f"T-12 line '{spot_line.label}': mapped {found.bucket}, not {spot_line.bucket}"
            )
        elif found.computed_total() != spot_line.total:
            misses.append(f"T-12 line '{spot_line.label}': total {found.computed_total()}")
        else:
            lines_correct += 1

    raised = {f.check_id for f in pkg.findings}
    problems = {f.check_id for f in pkg.findings if f.severity in ("warn", "block")}
    agreed = list(truth.findings_owner_agrees_with)
    caught = [c for c in agreed if c in raised]
    misses += [
        f"finding {c}: owner expected it, pipeline did not raise it"
        for c in agreed
        if c not in raised
    ]
    return RealScore(
        deal=pkg.deal_id,
        units_checked=len(truth.units_spot_checked),
        units_correct=units_correct,
        totals_checked=len(truth.stated_totals),
        totals_correct=totals_correct,
        lines_checked=len(truth.t12_lines_spot_checked),
        lines_correct=lines_correct,
        agreed_findings=agreed,
        caught_findings=caught,
        unconfirmed_findings=sorted(problems - set(agreed)),
        final_state=pkg.final_state,
        cost_usd=pkg.cost_usd,
        wall_seconds=pkg.wall_seconds,
        manual_entry_minutes=truth.manual_entry_minutes,
        review_minutes=truth.review_minutes,
        misses=misses,
    )


def evaluate_real(cfg: AppConfig, real_dir: Path, out_dir: Path) -> list[RealScore]:
    """Run the pipeline on every real deal and score the owner's spot checks."""
    deals = real_deal_dirs(real_dir)
    if not deals:
        raise CreScreenError(
            f"no real deals found: put each in {real_dir.as_posix()}/<name>/ with a {TRUTH_FILE}"
        )
    llm, tracker, _cache = make_llm(cfg)
    scores: list[RealScore] = []
    for deal in deals:
        truth = load_truth(deal / TRUTH_FILE)
        spent = tracker.run_total()
        pkg = run_deal(
            deal, llm=llm, cfg=cfg, tracker=tracker, out_dir=out_dir, learned_path=LEARNED
        )
        pkg = pkg.model_copy(update={"cost_usd": tracker.run_total() - spent})
        scores.append(score_real(pkg, truth))
    return scores


def _rate(correct: int, checked: int) -> str:
    return "n/a" if checked == 0 else f"{correct}/{checked}"


def render_markdown(scores: list[RealScore], meta: dict[str, str]) -> str:
    out = ["# Eval report: real", ""]
    out += [f"- {k}: {v}" for k, v in meta.items()]
    out += [
        "",
        "## Spot checks",
        "",
        "| Deal | Units | Stated totals | T-12 lines | Owner findings caught "
        "| Unconfirmed warn/block | State |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        out.append(
            f"| {s.deal} | {_rate(s.units_correct, s.units_checked)} "
            f"| {_rate(s.totals_correct, s.totals_checked)} "
            f"| {_rate(s.lines_correct, s.lines_checked)} "
            f"| {_rate(len(s.caught_findings), len(s.agreed_findings))} "
            f"| {', '.join(s.unconfirmed_findings) or 'none'} | {s.final_state} |"
        )
    out += [
        "",
        "## Time (owner's measured minutes)",
        "",
        "| Deal | Manual entry | Review in UI |",
        "| --- | --- | --- |",
    ]
    for s in scores:
        entry = s.manual_entry_minutes or "not measured"
        review = s.review_minutes or "not measured"
        out.append(f"| {s.deal} | {entry} | {review} |")
    out += ["", "## Misses", ""]
    misses = [f"- {s.deal}: {m}" for s in scores for m in s.misses]
    return "\n".join(out + (misses or ["None."])) + "\n"


def manual_timing(scores: list[RealScore]) -> dict[str, dict[str, Any]]:
    """Timing pairs for evals/manual_timing.yaml, so a report can cite them sans documents."""
    return {
        s.deal: {
            "manual_entry_minutes": None
            if s.manual_entry_minutes is None
            else str(s.manual_entry_minutes),
            "review_minutes": None if s.review_minutes is None else str(s.review_minutes),
        }
        for s in scores
    }


def scores_json(scores: list[RealScore]) -> str:
    return json.dumps([s.model_dump(mode="json") for s in scores], indent=2) + "\n"
