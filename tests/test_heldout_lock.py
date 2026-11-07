"""The held-out lock (SPEC 11.6, hard rule 10).

This is the only test that reads HELDOUT_MANIFEST.sha256 or hashes the held-out files. It never
parses them or runs the pipeline on them.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from synth.generate import MANIFEST_NAME, heldout_manifest, main, verify_manifest
from synth.plan import HELDOUT_DEALS
from synth.traps import DEAL_PLAN

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "synthetic"
LINE = re.compile(r"^[0-9a-f]{64}  deal_0(1[5-9]|20)/[a-z0-9_.]+$")


def test_the_committed_manifest_matches_the_held_out_files() -> None:
    assert verify_manifest(DATA) == []


def test_the_manifest_lists_five_files_for_each_of_the_six_held_out_deals() -> None:
    lines = (DATA / MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 30 and all(LINE.match(line) for line in lines)
    paths = [line.split("  ")[1] for line in lines]
    assert paths == sorted(paths)
    for number in HELDOUT_DEALS:
        names = {p.split("/")[1] for p in paths if p.startswith(f"deal_{number:03d}/")}
        assert {"meta.json", "proforma.yaml", "truth.json"} <= names
        assert any(n.startswith("rent_roll.") for n in names) and any(
            n.startswith("t12.") for n in names
        )


def test_only_the_six_held_out_deals_and_no_stray_deals_exist() -> None:
    folders = sorted(p.name for p in DATA.glob("deal_*"))
    assert folders == [f"deal_{n:03d}" for n in range(1, 21)]


# ---------- the lock logic itself, on a throwaway copy generated with a different seed ----------


@pytest.fixture(scope="module")
def sample_heldout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("heldout_sample")
    assert main(["--split", "heldout", "--deals", "15-20", "--seed", "77", "--out", str(out)]) == 0
    return out


def test_a_freshly_generated_set_verifies_clean(sample_heldout: Path) -> None:
    assert verify_manifest(sample_heldout) == []
    assert (sample_heldout / MANIFEST_NAME).read_text("utf-8") == heldout_manifest(sample_heldout)


def test_verification_catches_a_changed_a_missing_and_an_extra_file(
    sample_heldout: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(sample_heldout, copy)
    (copy / "deal_015" / "truth.json").write_bytes(b"{}")
    (copy / "deal_016" / "meta.json").unlink()
    (copy / "deal_017" / "notes.txt").write_text("hello", encoding="utf-8")
    assert verify_manifest(copy) == [
        "missing file: deal_016/meta.json",
        "unlisted file: deal_017/notes.txt",
        "changed file: deal_015/truth.json",
    ]


def test_a_missing_manifest_is_reported(tmp_path: Path) -> None:
    assert verify_manifest(tmp_path) == [f"{MANIFEST_NAME} is missing"]


def test_the_generator_refuses_to_overwrite_a_locked_set(
    sample_heldout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (sample_heldout / MANIFEST_NAME).read_text(encoding="utf-8")
    args = ["--split", "heldout", "--deals", "15-20", "--seed", "77", "--out", str(sample_heldout)]
    assert main(args) == 2
    assert "locked" in capsys.readouterr().err
    assert (sample_heldout / MANIFEST_NAME).read_text(encoding="utf-8") == before


def test_force_heldout_regenerates_and_rewrites_the_manifest(
    sample_heldout: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "forced"
    shutil.copytree(sample_heldout, copy)
    args = ["--split", "heldout", "--deals", "15-20", "--seed", "78", "--out", str(copy)]
    assert main([*args, "--force-heldout"]) == 0
    assert verify_manifest(copy) == []
    assert (copy / MANIFEST_NAME).read_text("utf-8") != (sample_heldout / MANIFEST_NAME).read_text(
        "utf-8"
    )


def test_held_out_deals_are_only_generated_together_with_their_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["--split", "heldout", "--deals", "15-16", "--seed", "77", "--out", str(tmp_path)])
        == 2
    )
    assert list(tmp_path.iterdir()) == []


def test_dev_generation_cannot_touch_held_out_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--split", "dev", "--deals", "15", "--seed", "7", "--out", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []


# ---------- rule 10, enforced mechanically ----------


def test_no_source_or_eval_code_names_a_held_out_deal_or_the_manifest() -> None:
    pattern = re.compile(r"deal_0?1[5-9]|deal_0?20|HELDOUT_MANIFEST")
    offenders: list[str] = []
    for folder in ("src", "evals"):
        for path in (ROOT / folder).rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_the_eval_runner_only_lists_dev_deals() -> None:
    from evals.run_eval import DEV_DEAL_NUMBERS

    assert list(DEV_DEAL_NUMBERS) == list(range(1, 15))


def test_the_plan_covers_every_held_out_variant_at_least_once() -> None:
    wanted = {
        "TRAP_RR_FOOTING",
        "TRAP_RR_DUP_UNIT",
        "TRAP_RR_HIDDEN_CONCESSION",
        "TRAP_T12_ONE_TIME_INCOME",
        "TRAP_PF_TAX_DROP",
    }
    used = {t for n in range(15, 20) for t in DEAL_PLAN[n]}
    assert wanted <= used
    assert DEAL_PLAN[20] == ()
    assert all(2 <= len(DEAL_PLAN[n]) <= 4 for n in range(15, 20))
