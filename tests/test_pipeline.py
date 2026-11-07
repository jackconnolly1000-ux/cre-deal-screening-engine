from __future__ import annotations

import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.config import load_config
from cre_screen.errors import CreScreenError, OverrideError
from cre_screen.mapping.llm_protocol import NullLLMClient
from cre_screen.pipeline import run_deal
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.findings import Override
from cre_screen.schemas.mapping import COAMapping, COAMappingBatch
from tests.fakes import FakeLLMClient

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
CFG = load_config()
NOW = datetime(2026, 11, 6, tzinfo=UTC)


def run(name: str, out: Path, **kw) -> DealPackage:
    return run_deal(DATA / name, llm=kw.pop("llm", NullLLMClient()), cfg=CFG, out_dir=out, **kw)


def test_clean_deal_is_ready_with_no_findings(tmp_path: Path) -> None:
    pkg = run("deal_001", tmp_path)
    assert pkg.final_state == "READY" and pkg.findings == [] and pkg.open_blocks() == []
    assert [t.to_state for t in pkg.gate_log] == ["CHECKS", "READY"]
    assert pkg.deal_id == "deal_001" and pkg.model_id == "claude-haiku-5-5"
    assert len(pkg.rent_roll.units) == 71 and len(pkg.t12.lines) == 16  # type: ignore[union-attr]
    assert pkg.proforma is not None and pkg.cost_usd == Decimal("0")


def test_footing_trap_deal_goes_through_the_real_pdf_reread(tmp_path: Path) -> None:
    pkg = run("deal_003", tmp_path)
    assert pkg.final_state == "REVIEW"
    assert [t.to_state for t in pkg.gate_log] == ["REEXTRACT", "CHECKS", "REVIEW"]
    (footing,) = [f for f in pkg.findings if f.check_id == "RR_FOOTING"]
    assert footing.severity == "block" and footing.details["cause"] == "document"
    assert {f.check_id for f in pkg.findings} == {
        "RR_FOOTING",
        "RR_ZERO_RENT_OCC",
        "T12_SPIKE",
    }


def test_package_is_written_atomically_and_round_trips(tmp_path: Path) -> None:
    pkg = run("deal_004", tmp_path)
    path = tmp_path / "deal_004" / "package.json"
    assert path.exists() and not list(tmp_path.rglob("*.tmp"))
    assert DealPackage.model_validate_json(path.read_bytes()) == pkg


def test_extraction_cache_goes_under_the_out_dir_never_into_data(tmp_path: Path) -> None:
    before = sorted(p.name for p in (DATA / "deal_001").iterdir())
    run("deal_001", tmp_path)
    assert sorted(p.name for p in (DATA / "deal_001").iterdir()) == before
    assert list((tmp_path / "cache" / "extract").glob("*.json"))


def test_overrides_clear_the_block_and_are_stored(tmp_path: Path) -> None:
    note = Override(
        check_id="RR_FOOTING", action="override", target="*", note="seller confirmed totals", at=NOW
    )
    pkg = run("deal_003", tmp_path, overrides=[note])
    assert pkg.final_state == "READY" and pkg.overrides == [note]
    assert {f.status for f in pkg.findings if f.check_id == "RR_FOOTING"} == {"overridden"}


def test_data_changing_overrides_are_rejected_with_a_clear_error(tmp_path: Path) -> None:
    edit = Override(
        check_id="RR_FOOTING", action="edit_value", target="101", note="typo in the rent", at=NOW
    )
    with pytest.raises(OverrideError, match="review UI"):
        run("deal_003", tmp_path, overrides=[edit])


def test_llm_is_used_only_for_labels_the_synonym_table_misses(tmp_path: Path) -> None:
    canned = FakeLLMClient(
        {
            ("coa_map", frozenset(["Lease Buyout Fee"])): COAMappingBatch(
                mappings=[
                    COAMapping(
                        line_label="Lease Buyout Fee",
                        bucket="OTHER_INCOME",
                        is_nonrecurring=True,
                        rationale="one-time",
                    )
                ]
            ),
            ("coa_map", frozenset(["Insurance Proceeds"])): COAMappingBatch(
                mappings=[
                    COAMapping(
                        line_label="Insurance Proceeds",
                        bucket="OTHER_INCOME",
                        is_nonrecurring=True,
                        rationale="one-time",
                    )
                ]
            ),
        }
    )
    pkg = run("deal_005", tmp_path, llm=canned)
    assert len(canned.calls_for("coa_map")) == 1 and canned.calls_for("header_map") == []
    assert {f.check_id for f in pkg.findings} >= {"RR_DUP_UNIT", "T12_NONRECURRING", "X_RR_VS_T3"}


def test_as_of_argument_overrides_the_document_date(tmp_path: Path) -> None:
    from datetime import date

    pkg = run("deal_001", tmp_path, as_of=date(2030, 1, 1))
    assert pkg.rent_roll.as_of_date == date(2030, 1, 1)
    assert "RR_EXPIRED_LEASE" in {f.check_id for f in pkg.findings}


def test_deal_without_t12_or_proforma_still_runs(tmp_path: Path) -> None:
    deal = tmp_path / "data" / "deal_001"
    deal.mkdir(parents=True)
    shutil.copy(DATA / "deal_001" / "rent_roll.pdf", deal / "rent_roll.pdf")
    pkg = run_deal(deal, llm=NullLLMClient(), cfg=CFG, out_dir=tmp_path / "out")
    assert pkg.t12 is None and pkg.proforma is None and pkg.final_state == "READY"


def test_missing_rent_roll_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(CreScreenError, match="needs exactly one rent_roll"):
        run_deal(tmp_path, llm=NullLLMClient(), cfg=CFG, out_dir=tmp_path / "out")


def test_ambiguous_inputs_are_rejected(tmp_path: Path) -> None:
    deal = tmp_path / "deal_x"
    deal.mkdir()
    shutil.copy(DATA / "deal_001" / "rent_roll.pdf", deal / "rent_roll.pdf")
    (deal / "rent_roll.xlsx").write_bytes(b"")
    with pytest.raises(CreScreenError, match="exactly one"):
        run_deal(deal, llm=NullLLMClient(), cfg=CFG, out_dir=tmp_path / "out")
