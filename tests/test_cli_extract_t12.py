from __future__ import annotations

from pathlib import Path

import pytest

from cre_screen import runtime
from cre_screen.__main__ import main

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_LLM_CACHE_MODE", "replay")


def test_clean_deal_lists_every_line_with_bucket_and_mapping_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["extract-t12", str(DATA / "deal_001")]) == 0
    out = capsys.readouterr().out
    assert "Lines: 16" in out
    assert "GROSS_POTENTIAL_RENT" in out and "synonym_table" in out
    assert "Mapping sources: synonym_table=16" in out
    assert "T-12 footing: PASS" in out
    assert "Cost this run: $0.00 (cache mode: replay)" in out


def test_footing_trap_deal_reports_the_failing_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-t12", str(DATA / "deal_004")]) == 0
    out = capsys.readouterr().out
    assert "T-12 footing: FAIL" in out and "[block]" in out and "Landscaping" in out


def test_replay_miss_exits_4_with_a_clear_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)  # an empty cache, whatever gets recorded
    # deal_005 has a one-time income label outside the synonym table, so it needs the LLM
    assert main(["extract-t12", str(DATA / "deal_005")]) == 4
    assert "cache miss in replay mode: task=coa_map" in capsys.readouterr().err


def test_missing_t12_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-t12", str(tmp_path)]) == 2
    assert "needs exactly one t12" in capsys.readouterr().err
