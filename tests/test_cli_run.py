from __future__ import annotations

from pathlib import Path

import pytest

from cre_screen import runtime
from cre_screen.__main__ import main

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Replay mode, an empty LLM cache, and a scratch working directory for out/."""
    monkeypatch.setenv("CRE_LLM_CACHE_MODE", "replay")
    monkeypatch.setattr(runtime, "REPO_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_clean_deal_ends_ready(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(DATA / "deal_001")]) == 0
    out = capsys.readouterr().out
    assert "Gate: FOOTING -> CHECKS -> READY" in out
    assert "Findings: 0 block, 0 warn, 0 info   Open blocks: 0" in out
    assert "Final state: READY" in out
    assert "Cost this run: $0.00 (cache mode: replay)" in out


def test_trap_deal_lists_findings_by_severity_with_the_footing_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", str(DATA / "deal_003")]) == 0
    out = capsys.readouterr().out
    assert "Gate: FOOTING -> REEXTRACT -> CHECKS -> REVIEW" in out
    assert "[block] RR_FOOTING:" in out and "(cause: document)" in out
    assert out.index("[block]") < out.index("[warn]") < out.index("[info]")
    assert "Final state: REVIEW" in out and "Open blocks: 1" in out


def test_screen_is_an_alias_for_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["screen", str(DATA / "deal_002")]) == 0
    assert "Final state: READY" in capsys.readouterr().out


def test_package_is_written_under_out(isolated: Path) -> None:
    assert main(["run", str(DATA / "deal_001")]) == 0
    assert (isolated / "out" / "deal_001" / "package.json").exists()


def test_as_of_argument_is_used(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(DATA / "deal_001"), "--as-of", "2030-01-01"]) == 0
    out = capsys.readouterr().out
    assert "as of 2030-01-01" in out and "RR_EXPIRED_LEASE" in out


def test_export_writes_a_workbook_for_a_clean_deal(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = isolated / "x.xlsx"
    assert main(["run", str(DATA / "deal_001"), "--export", str(target)]) == 0
    assert target.exists() and "Workbook:" in capsys.readouterr().out


def test_export_is_refused_while_blocks_are_open(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = isolated / "x.xlsx"
    assert main(["run", str(DATA / "deal_003"), "--export", str(target)]) == 5
    assert not target.exists() and "export refused: 1 open block" in capsys.readouterr().err


def test_allow_open_blocks_exports_anyway(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = isolated / "out" / "deal_003.xlsx"
    code = main(["run", str(DATA / "deal_003"), "--export", str(target), "--allow-open-blocks"])
    assert code == 0 and target.exists()
    assert "with 1 open block finding(s)" in capsys.readouterr().out


def test_export_command_builds_the_workbook_from_a_saved_package(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", str(DATA / "deal_001")]) == 0
    package = isolated / "out" / "deal_001" / "package.json"
    target = isolated / "from_package.xlsx"
    assert main(["export", str(package), "--out", str(target)]) == 0
    assert target.exists()


def test_export_command_rejects_a_missing_package(isolated: Path, capsys) -> None:
    assert main(["export", str(isolated / "nope.json"), "--out", str(isolated / "x.xlsx")]) == 2
    assert "cannot read package" in capsys.readouterr().err


def test_unrecorded_llm_call_exits_4(capsys: pytest.CaptureFixture[str]) -> None:
    # deal_005 has a one-time income label the synonym table cannot place; the cache is empty here
    assert main(["run", str(DATA / "deal_005")]) == 4
    assert "cache miss in replay mode: task=coa_map" in capsys.readouterr().err


def test_missing_inputs_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(tmp_path)]) == 2
    assert "needs exactly one rent_roll" in capsys.readouterr().err


def test_a_run_cap_below_the_worst_case_exits_3_before_any_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CRE_LLM_CACHE_MODE", "record")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["run", str(DATA / "deal_005"), "--run-cap", "0.0000001"]) == 3
    assert "run cap" in capsys.readouterr().err


# ---------- the app command ----------


def test_app_launches_streamlit_on_the_review_script(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import subprocess

    from cre_screen import __main__ as cli

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert main(["app"]) == 0
    (command,) = calls
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert command[4].replace("\\", "/").endswith("cre_screen/app/streamlit_app.py")
    assert Path(command[4]).exists()
    assert command[-2:] == ["--browser.gatherUsageStats", "false"]
    assert "Starting the review UI" in capsys.readouterr().out


def test_app_passes_the_port_and_the_servers_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from cre_screen import __main__ as cli

    seen: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_k: seen.append(command) or subprocess.CompletedProcess(command, 7),
    )
    assert main(["app", "--port", "8123"]) == 7
    assert seen[0][-2:] == ["--server.port", "8123"]


def test_budget_sums_the_ledger_by_model_and_task(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = isolated / "ledger.jsonl"
    rows = [
        ("claude-haiku-5-5", "coa_map", "0.0002557"),
        ("claude-haiku-5-5", "vision_page", "0.0019000"),
        ("claude-sonnet-5-5", "coa_map", "0.5000000"),
    ]
    ledger.write_text(
        "".join(
            f'{{"model": "{m}", "task": "{t}", "cost_usd": "{c}"}}' + chr(10) for m, t, c in rows
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "LEDGER", ledger)
    assert main(["budget"]) == 0
    out = capsys.readouterr().out
    assert "(3 API attempts)" in out
    assert "claude-haiku-5-5: $0.0022" in out and "claude-sonnet-5-5: $0.5000" in out
    assert "coa_map: $0.5003" in out
    assert "Lifetime spend: $0.5022 of $10.00 cap" in out
    assert "Remaining: $9.4978" in out


def test_budget_with_no_ledger_reports_zero(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runtime, "LEDGER", isolated / "missing.jsonl")
    assert main(["budget"]) == 0
    assert "Lifetime spend: $0.0000 of $10.00 cap" in capsys.readouterr().out
