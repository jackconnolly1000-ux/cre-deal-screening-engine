from __future__ import annotations

from pathlib import Path

import pytest

from cre_screen.__main__ import main

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def test_clean_deal_reports_units_totals_and_pass(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-rr", str(DATA / "deal_001")]) == 0
    out = capsys.readouterr().out
    assert "Units found: 71" in out
    assert "stated $" in out and "computed all units $" in out
    assert "Footing: PASS" in out


def test_footing_trap_deal_reports_fail_with_the_delta(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-rr", str(DATA / "deal_003")]) == 0
    out = capsys.readouterr().out
    assert "Units found: 28" in out
    assert "Footing: FAIL" in out
    assert "[block]" in out and "-36000.00" in out


def test_missing_input_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-rr", str(tmp_path)]) == 2
    assert "needs exactly one rent_roll" in capsys.readouterr().err


def test_as_of_override_is_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract-rr", str(DATA / "deal_002"), "--as-of", "2026-12-31"]) == 0
    assert "As of: 2026-12-31" in capsys.readouterr().out
