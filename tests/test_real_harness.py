from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from cre_screen.config import load_config
from cre_screen.errors import ConfigError
from cre_screen.mapping.cost import CostTracker
from cre_screen.pipeline import run_deal
from evals import run_eval
from evals.real import (
    ManualTruth,
    evaluate_real,
    load_truth,
    manual_timing,
    real_deal_dirs,
    render_markdown,
    score_real,
)
from tests.fakes import CannedCoaLLM

D = Decimal
CFG = load_config()
SYNTH = Path(__file__).resolve().parents[1] / "data" / "synthetic"

# deal_003 stands in for a public deal: its truth is known, so a hand-written
# truth_manual.yaml can be checked against it.
TRUTH = {
    "source_url": "https://example.com/public-rent-roll.pdf",
    "retrieved": "2026-11-15",
    "units_spot_checked": [
        {"unit_id": "102", "in_place_rent": "1760.00", "market_rent": "1814.00"},
        {"unit_id": "104", "in_place_rent": None, "market_rent": "1481.00"},
    ],
    "stated_totals": {"in_place_rent": "73703.00", "market_rent": "45269.00"},
    "t12_lines_spot_checked": [
        {"label": "R/E Taxes", "bucket": "REAL_ESTATE_TAXES", "total": "60088.00"},
    ],
    "findings_owner_agrees_with": ["RR_FOOTING", "T12_SPIKE"],
    "manual_entry_minutes": 95,
    "review_minutes": "12.5",
}


@pytest.fixture
def real_dir(tmp_path: Path) -> Path:
    deal = tmp_path / "real" / "maple_court"
    deal.mkdir(parents=True)
    for name in ("rent_roll.pdf", "t12.pdf", "proforma.yaml"):
        shutil.copy(SYNTH / "deal_003" / name, deal / name)
    (deal / "truth_manual.yaml").write_text(
        yaml.safe_dump(TRUTH, sort_keys=False), encoding="utf-8"
    )
    return tmp_path / "real"


def package(real_dir: Path, tmp_path: Path):
    return run_deal(real_dir / "maple_court", llm=CannedCoaLLM(), cfg=CFG, out_dir=tmp_path / "out")


# ---------- loading the owner's file ----------


def test_the_manual_truth_file_loads_with_exact_decimals(real_dir: Path) -> None:
    truth = load_truth(real_dir / "maple_court" / "truth_manual.yaml")
    assert truth.units_spot_checked[0].in_place_rent == D("1760.00")
    assert truth.units_spot_checked[1].in_place_rent is None
    assert truth.stated_totals == {"in_place_rent": D("73703.00"), "market_rent": D("45269.00")}
    assert truth.t12_lines_spot_checked[0].bucket == "REAL_ESTATE_TAXES"
    assert truth.findings_owner_agrees_with == ["RR_FOOTING", "T12_SPIKE"]
    assert truth.manual_entry_minutes == D("95") and truth.review_minutes == D("12.5")


def test_the_spec_example_file_validates() -> None:
    example = yaml.safe_load(
        """
source_url: "https://..."
retrieved: "2026-11-15"
units_spot_checked:
  - {unit_id: "101", in_place_rent: "1250.00", market_rent: "1300.00"}
stated_totals: {in_place_rent: "56250.00"}
t12_lines_spot_checked:
  - {label: "Real Estate Taxes", bucket: REAL_ESTATE_TAXES, total: "98450.00"}
findings_owner_agrees_with: []
manual_entry_minutes: null
review_minutes: null
"""
    )
    truth = ManualTruth.model_validate(example)
    assert truth.manual_entry_minutes is None and truth.findings_owner_agrees_with == []


@pytest.mark.parametrize(
    ("section", "bad", "message"),
    [
        ("units_spot_checked", [{"unit_id": "1", "in_place_rent": 1250.5}], "YAML float"),
        ("stated_totals", {"in_place_rent": 99.5}, "YAML float"),
        ("t12_lines_spot_checked", [{"label": "x", "bucket": "NOPE", "total": "1.00"}], "bucket"),
        ("findings_owner_agrees_with", ["NOT_A_CHECK"], "findings_owner_agrees_with"),
        ("units_spot_checked", [{"unit_id": "1", "rent": "5"}], "rent"),
    ],
)
def test_bad_manual_truth_is_rejected_with_a_useful_message(
    real_dir: Path, section: str, bad, message: str
) -> None:
    path = real_dir / "maple_court" / "truth_manual.yaml"
    path.write_text(yaml.safe_dump({**TRUTH, section: bad}), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_truth(path)


def test_unknown_top_level_key_and_missing_file_are_errors(real_dir: Path, tmp_path: Path) -> None:
    path = real_dir / "maple_court" / "truth_manual.yaml"
    path.write_text(yaml.safe_dump({**TRUTH, "surprise": 1}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_truth(path)
    with pytest.raises(ConfigError, match="cannot read"):
        load_truth(tmp_path / "nope.yaml")


def test_only_folders_with_a_truth_file_count_as_real_deals(real_dir: Path, tmp_path: Path) -> None:
    (real_dir / "no_truth").mkdir()
    (real_dir / "stray.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in real_deal_dirs(real_dir)] == ["maple_court"]
    assert real_deal_dirs(tmp_path / "absent") == []


# ---------- scoring ----------


def test_correct_spot_checks_score_in_full(real_dir: Path, tmp_path: Path) -> None:
    truth = load_truth(real_dir / "maple_court" / "truth_manual.yaml")
    score = score_real(package(real_dir, tmp_path), truth)
    assert (score.units_correct, score.units_checked) == (2, 2)
    assert (score.totals_correct, score.totals_checked) == (2, 2)
    assert (score.lines_correct, score.lines_checked) == (1, 1)
    assert score.caught_findings == ["RR_FOOTING", "T12_SPIKE"] and score.misses == []
    assert score.unconfirmed_findings == ["RR_ZERO_RENT_OCC"], "raised but not on the owner's list"
    assert score.final_state == "REVIEW" and score.deal == "maple_court"
    assert (score.manual_entry_minutes, score.review_minutes) == (D("95"), D("12.5"))


def test_a_wrong_value_a_missing_unit_and_a_missed_finding_are_each_reported(
    real_dir: Path, tmp_path: Path
) -> None:
    broken = {
        **TRUTH,
        "units_spot_checked": [
            {"unit_id": "102", "in_place_rent": "1761.00"},
            {"unit_id": "999", "in_place_rent": "1.00"},
        ],
        "stated_totals": {"in_place_rent": "1.00"},
        "t12_lines_spot_checked": [
            {"label": "R/E Taxes", "bucket": "INSURANCE", "total": "60088.00"},
            {"label": "R/E Taxes", "bucket": "REAL_ESTATE_TAXES", "total": "1.00"},
            {"label": "Nope", "bucket": "MARKETING", "total": "1.00"},
        ],
        "findings_owner_agrees_with": ["RR_DUP_UNIT"],
    }
    path = real_dir / "maple_court" / "truth_manual.yaml"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    score = score_real(package(real_dir, tmp_path), load_truth(path))
    assert (score.units_correct, score.totals_correct, score.lines_correct) == (0, 0, 0)
    text = "\n".join(score.misses)
    assert "unit 102: in_place_rent 1760.00 != 1761.00" in text
    assert "unit 999: not extracted" in text
    assert "stated total in_place_rent: read 73703.00, printed 1.00" in text
    assert "mapped REAL_ESTATE_TAXES, not INSURANCE" in text
    assert "T-12 line 'Nope': not extracted" in text
    assert "finding RR_DUP_UNIT: owner expected it, pipeline did not raise it" in text


def test_a_blank_expected_rent_is_not_checked(real_dir: Path, tmp_path: Path) -> None:
    only_market = {**TRUTH, "units_spot_checked": [{"unit_id": "102", "market_rent": "1814.00"}]}
    path = real_dir / "maple_court" / "truth_manual.yaml"
    path.write_text(yaml.safe_dump(only_market), encoding="utf-8")
    assert score_real(package(real_dir, tmp_path), load_truth(path)).units_correct == 1


# ---------- running the whole set ----------


def test_evaluate_real_runs_every_deal_and_tracks_cost(
    real_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = CostTracker(CFG.models, tmp_path / "ledger.jsonl", run_id="t")
    monkeypatch.setattr("evals.real.make_llm", lambda cfg: (CannedCoaLLM(), tracker, None))
    scores = evaluate_real(CFG, real_dir, tmp_path / "out")
    assert [s.deal for s in scores] == ["maple_court"] and scores[0].cost_usd == 0


def test_no_real_deals_is_a_clear_error(tmp_path: Path) -> None:
    from cre_screen.errors import CreScreenError

    with pytest.raises(CreScreenError, match="no real deals found"):
        evaluate_real(CFG, tmp_path / "empty", tmp_path / "out")


def test_the_markdown_report_shows_spot_checks_time_and_misses(
    real_dir: Path, tmp_path: Path
) -> None:
    truth = load_truth(real_dir / "maple_court" / "truth_manual.yaml")
    score = score_real(package(real_dir, tmp_path), truth)
    text = render_markdown([score], {"split": "real", "git_sha": "abc"})
    assert "# Eval report: real" in text and "git_sha: abc" in text
    assert "| maple_court | 2/2 | 2/2 | 1/1 | 2/2 | RR_ZERO_RENT_OCC | REVIEW |" in text
    assert "| maple_court | 95 | 12.5 |" in text and "None." in text


def test_unmeasured_time_is_stated_not_invented(real_dir: Path, tmp_path: Path) -> None:
    path = real_dir / "maple_court" / "truth_manual.yaml"
    path.write_text(
        yaml.safe_dump({**TRUTH, "manual_entry_minutes": None, "review_minutes": None}),
        encoding="utf-8",
    )
    score = score_real(package(real_dir, tmp_path), load_truth(path))
    assert "not measured" in render_markdown([score], {})
    assert manual_timing([score]) == {
        "maple_court": {"manual_entry_minutes": None, "review_minutes": None}
    }


# ---------- the CLI ----------


@pytest.fixture
def cli(real_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tracker = CostTracker(CFG.models, tmp_path / "ledger.jsonl", run_id="t")
    monkeypatch.setattr("evals.real.make_llm", lambda cfg: (CannedCoaLLM(), tracker, None))
    monkeypatch.setattr(run_eval, "REAL_DIR", real_dir)
    monkeypatch.setattr(run_eval, "REPO_ROOT", tmp_path)
    return tmp_path


def test_split_real_writes_reports_and_optionally_the_timing_pairs(
    cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = cli / "reports"
    assert run_eval.main(["--split", "real", "--out", str(out), "--update-timing"]) == 0
    printed = capsys.readouterr().out
    assert "real: 1 deal(s)" in printed and "units 2/2, totals 2/2, T-12 lines 1/1" in printed
    assert sorted(p.suffix for p in out.iterdir()) == [".json", ".md"]
    reports = json.loads(next(out.glob("*_real.json")).read_text("utf-8"))
    assert reports[0]["deal"] == "maple_court"
    timing = yaml.safe_load((cli / "evals" / "manual_timing.yaml").read_text("utf-8"))
    assert timing == {"maple_court": {"manual_entry_minutes": "95", "review_minutes": "12.5"}}


def test_split_real_without_any_real_deals_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_eval, "REAL_DIR", tmp_path / "none")
    assert run_eval.main(["--split", "real", "--out", str(tmp_path)]) == 2
    assert "no real deals found" in capsys.readouterr().err
