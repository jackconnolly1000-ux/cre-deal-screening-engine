from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest

from cre_screen.config import load_config
from cre_screen.mapping.cost import CostTracker
from cre_screen.schemas.deal import DealScore
from evals import run_eval
from evals.report import aggregate, dominant_provenance, rate, render_markdown, write_reports
from tests.fake_vision import PerfectOcr, clean_transcriptions
from tests.fakes import CannedCoaLLM

D = Decimal
CFG = load_config()


DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@lru_cache(maxsize=1)
def _transcriptions() -> dict:
    """Flawless transcriptions of the committed scanned dev deals (built once, offline)."""
    return {
        key: page
        for n in (10, 11)
        for key, page in clean_transcriptions(n, DATA / f"deal_{n:03d}" / "rent_roll.pdf").items()
    }


def offline_llm() -> PerfectOcr:
    """Perfect OCR for the scans and a canned answer for the one-time income label."""
    return PerfectOcr(_transcriptions(), fallback=CannedCoaLLM())


def _patch_llm(monkeypatch: pytest.MonkeyPatch, ledger: Path) -> CostTracker:
    tracker = CostTracker(CFG.models, ledger, run_id="t")
    monkeypatch.setattr(run_eval, "make_llm", lambda cfg: (offline_llm(), tracker, None))
    return tracker


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CostTracker:
    return _patch_llm(monkeypatch, tmp_path / "ledger.jsonl")


# ---------- the whole dev split, end to end ----------


@pytest.fixture(scope="module")
def scores(tmp_path_factory: pytest.TempPathFactory) -> list[DealScore]:
    root = tmp_path_factory.mktemp("eval")
    with pytest.MonkeyPatch.context() as mp:
        _patch_llm(mp, root / "ledger.jsonl")
        result, cost = run_eval.evaluate(CFG, out_dir=root / "out")
    assert cost == 0
    return result


def test_every_dev_deal_is_scored_and_held_out_deals_are_not_listed(scores) -> None:
    assert [s.deal_id for s in scores] == [f"deal_{n:03d}" for n in range(1, 15)]
    assert {s.split for s in scores} == {"dev"}


def test_every_planted_error_is_caught_with_no_false_positives(scores) -> None:
    overall = aggregate(scores)["overall"]
    assert overall["planted"] == 36 and overall["caught"] == 36
    assert overall["false_positives"] == 0
    assert [s.misses for s in scores if s.misses] == []


def test_clean_controls_have_zero_warn_or_block_findings(scores) -> None:
    clean = [s for s in scores if s.planted == 0]
    assert [s.deal_id for s in clean] == ["deal_001", "deal_002"]
    assert all(s.clean_control_findings == 0 for s in clean)


def test_extraction_is_exact_on_the_synthetic_documents(scores) -> None:
    overall = aggregate(scores)["overall"]
    assert overall["numeric_accuracy"] == "1.0000" and overall["unit_recall"] == "1.0000"
    assert overall["phantom_units"] == 0 and overall["coa_accuracy"] == "1.0000"


# ---------- aggregation and reports ----------


def score(**kw) -> DealScore:
    base = dict(
        deal_id="deal_x",
        split="dev",
        provenance_mix={"text_layer": 10},
        numeric_fields_total=10,
        numeric_fields_correct=9,
        units_in_doc=2,
        units_found=2,
        phantom_units=0,
        coa_dollars_total=D("100.00"),
        coa_dollars_correct=D("80.00"),
        planted=2,
        caught=1,
        false_positives=1,
        clean_control_findings=0,
        cost_usd=D("0.01"),
        wall_seconds=1.0,
        misses=["TRAP_A: nothing raised"],
    )
    return DealScore(**{**base, **kw})


def test_rate_is_four_places_and_an_empty_denominator_is_perfect() -> None:
    assert rate(1, 3) == D("0.3333") and rate(0, 0) == D("1.0000")


def test_aggregate_sums_counts_and_derives_rates() -> None:
    a = aggregate([score(), score(deal_id="b", numeric_fields_correct=10, caught=2)])["overall"]
    assert (a["deals"], a["planted"], a["caught"]) == (2, 4, 3)
    assert a["numeric_accuracy"] == "0.9500" and a["coa_accuracy"] == "0.8000"
    assert a["catch_rate"] == "0.7500" and a["cost_usd"] == "0.02"


def test_aggregate_is_split_by_split_and_by_provenance() -> None:
    agg = aggregate(
        [
            score(split="dev"),
            score(split="heldout", provenance_mix={"llm_vision": 10}),
            score(provenance_mix={}),
        ]
    )
    assert set(agg["by_split"]) == {"dev", "heldout"}
    assert set(agg["by_provenance"]) == {"text_layer", "llm_vision", "none"}
    assert dominant_provenance(score(provenance_mix={"excel": 3, "text_layer": 5})) == "text_layer"


def test_markdown_report_lists_aggregates_per_deal_rows_and_every_miss() -> None:
    text = render_markdown([score()], {"split": "dev", "git_sha": "abc1234"})
    assert "# Eval report: dev" in text and "git_sha: abc1234" in text
    assert "| deal_x | 9/10 | 2/2 | 0 |" in text
    assert "- deal_x: TRAP_A: nothing raised" in text
    assert "## By split" in text and "## By provenance" in text


def test_report_files_are_written_with_the_given_stem(tmp_path: Path) -> None:
    json_path, md_path = write_reports(
        [score()], tmp_path, "2026-11-06_abc1234_dev", {"split": "dev"}
    )
    assert json_path.name == "2026-11-06_abc1234_dev.json" and md_path.suffix == ".md"
    assert DealScore.model_validate(json.loads(json_path.read_text("utf-8"))[0]) == score()
    assert not list(tmp_path.glob("*.tmp"))


def test_baseline_records_the_numbers_the_regression_test_compares() -> None:
    base = run_eval.baseline_from([score(), score(deal_id="b")])
    assert base["caught"] == 2 and base["planted"] == 4 and base["false_positives"] == 2
    assert base["numeric_accuracy"] == "0.9000"
    assert base["per_deal"] == {
        "deal_x": {"planted": 2, "caught": 1},
        "b": {"planted": 2, "caught": 1},
    }


# ---------- the CLI ----------


@pytest.mark.parametrize("split", ["heldout", "all"])
def test_held_out_and_combined_splits_are_not_available_yet(
    split: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_eval.main(["--split", split]) == 2
    assert "not available yet" in capsys.readouterr().err


def test_final_flag_is_refused_on_dev(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_eval.main(["--split", "dev", "--final"]) == 2
    assert "held-out" in capsys.readouterr().err


def test_dev_run_writes_reports_and_can_rewrite_the_baseline(
    stub_llm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline_dev.json"
    monkeypatch.setattr(run_eval, "BASELINE_PATH", baseline)
    monkeypatch.setattr(run_eval, "REPO_ROOT", tmp_path)
    out = tmp_path / "reports"
    code = run_eval.main(["--split", "dev", "--out", str(out), "--update-baseline"])
    printed = capsys.readouterr().out
    assert code == 0
    assert "caught 36/36" in printed and "false positives 0" in printed
    assert baseline.exists() and json.loads(baseline.read_text("utf-8"))["caught"] == 36
    assert sorted(p.suffix for p in out.iterdir()) == [".json", ".md"]
    assert "baseline rewritten" in printed


def test_a_replay_miss_exits_4_and_asks_for_a_recording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cre_screen.errors import CacheMissError

    def boom(cfg, **kw):
        raise CacheMissError("coa_map", "abc")

    monkeypatch.setattr(run_eval, "evaluate", boom)
    assert run_eval.main(["--split", "dev", "--cache-mode", "replay"]) == 4
    assert "CRE_LLM_CACHE_MODE=record" in capsys.readouterr().err


def test_a_scan_with_no_readable_as_of_date_is_rerun_with_the_truths_date_and_noted(
    stub_llm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vision prompt returns the table only, so a scan's "As of" title line is lost. The
    real CLI asks the operator for --as-of; the harness plays the operator and says so."""
    untitled = {
        key: page.model_copy(update={"rows": [r for r in page.rows if r.row_kind != "other"]})
        for key, page in _transcriptions().items()
    }
    llm = PerfectOcr(untitled, fallback=CannedCoaLLM())
    monkeypatch.setattr(run_eval, "make_llm", lambda cfg: (llm, stub_llm, None))
    notes: list[str] = []
    scores, _ = run_eval.evaluate(CFG, out_dir=tmp_path / "out", notes=notes)
    assert notes == [
        "deal_010: no as-of date read; supplied 2026-10-31",
        "deal_011: no as-of date read; supplied 2026-07-31",
    ]
    overall = aggregate(scores)["overall"]
    assert overall["caught"] == 36 and overall["numeric_accuracy"] == "1.0000"


def test_the_dev_report_states_which_as_of_dates_the_harness_supplied(
    stub_llm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    untitled = {
        key: page.model_copy(update={"rows": [r for r in page.rows if r.row_kind != "other"]})
        for key, page in _transcriptions().items()
    }
    llm = PerfectOcr(untitled, fallback=CannedCoaLLM())
    monkeypatch.setattr(run_eval, "make_llm", lambda cfg: (llm, stub_llm, None))
    monkeypatch.setattr(run_eval, "BASELINE_PATH", tmp_path / "b.json")
    assert run_eval.main(["--split", "dev", "--out", str(tmp_path / "reports")]) == 0
    report = next((tmp_path / "reports").glob("*_dev.md")).read_text(encoding="utf-8")
    assert "as_of_supplied_by_harness: deal_010" in report and "deal_011" in report
