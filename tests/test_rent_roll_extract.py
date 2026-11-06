from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.config import AppConfig, load_config
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.ingest.pdf_tables import extract_pdf_table
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.schemas.common import to_cents
from cre_screen.schemas.deal import DealTruth
from cre_screen.schemas.rent_roll import RentRoll
from tests.fakes import FakeLLMClient

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
DEALS = [f"deal_{n:03d}" for n in range(1, 6)]
D = Decimal


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return load_config()


@pytest.fixture(scope="module")
def extracted(cfg: AppConfig) -> dict[str, tuple[RentRoll, DealTruth, FakeLLMClient]]:
    out = {}
    for name in DEALS:
        folder = DATA / name
        fake = FakeLLMClient()
        table = extract_pdf_table(folder / "rent_roll.pdf", cfg.rr_headers)
        header_map, _ = map_headers(table.headers, [], cfg.rr_headers, fake)
        rr = build_rent_roll(table, header_map, cfg, llm=fake)
        truth = DealTruth.model_validate_json((folder / "truth.json").read_bytes())
        out[name] = (rr, truth, fake)
    return out


@pytest.mark.parametrize("name", DEALS)
def test_every_truth_unit_is_found_in_order_with_zero_phantoms(extracted, name: str) -> None:
    rr, truth, _ = extracted[name]
    assert [u.unit_id for u in rr.units] == [u.unit_id for u in truth.units]


@pytest.mark.parametrize("name", DEALS)
def test_all_fields_match_the_document_view_truth_exactly(extracted, name: str) -> None:
    rr, truth, _ = extracted[name]
    for got, want in zip(rr.units, truth.units, strict=True):
        for field in (
            "unit_type",
            "beds",
            "baths",
            "sq_ft",
            "status",
            "in_place_rent",
            "market_rent",
            "lease_start",
            "lease_end",
        ):
            assert getattr(got, field) == getattr(want, field), (name, want.unit_id, field)


@pytest.mark.parametrize("name", DEALS)
def test_money_fields_are_exact_decimals_with_two_places(extracted, name: str) -> None:
    rr, _, _ = extracted[name]
    for u in rr.units:
        for value in (u.in_place_rent, u.market_rent):
            if value is not None:
                assert isinstance(value, Decimal)
                assert value == to_cents(value)


@pytest.mark.parametrize("name", DEALS)
def test_stated_totals_property_and_as_of_match(extracted, name: str) -> None:
    rr, truth, _ = extracted[name]
    meta = json.loads((DATA / name / "meta.json").read_text(encoding="utf-8"))
    assert rr.stated_totals == {k: D(v) for k, v in truth.rr_stated_totals.items()}
    assert set(rr.stated_total_citations) == {"in_place_rent", "market_rent"}
    assert rr.property_name == meta["property_name"]
    assert rr.as_of_date == truth.as_of_date
    assert rr.extraction_warnings == []
    assert rr.header_map.source == "synonym_table"
    assert rr.source_format == "pdf_text"


@pytest.mark.parametrize("name", DEALS)
def test_scope_of_the_only_subtotal_is_grand_total_covering_all_units(extracted, name: str) -> None:
    rr, _, _ = extracted[name]
    assert {s.scope for s in rr.subtotals} == {"grand_total"}
    assert all(s.covers_unit_ids == [u.unit_id for u in rr.units] for s in rr.subtotals)


@pytest.mark.parametrize("name", DEALS)
def test_every_populated_field_has_a_pdf_citation_on_the_right_page(extracted, name: str) -> None:
    rr, truth, _ = extracted[name]
    for got, want in zip(rr.units, truth.units, strict=True):
        assert got.citations["unit_id"].page == want.page
        assert got.citations["unit_id"].raw_text == want.unit_id
        for field, cite in got.citations.items():
            assert cite.provenance == "text_layer", field
            assert cite.bbox is not None and cite.source_file == "rent_roll.pdf"
        if want.in_place_rent is not None:
            assert "in_place_rent" in got.citations
        else:
            assert "in_place_rent" not in got.citations


@pytest.mark.parametrize("name", DEALS)
def test_no_llm_calls_on_the_synthetic_classic_layout(extracted, name: str) -> None:
    _, _, fake = extracted[name]
    assert fake.calls == []


def test_concession_notes_become_grounded_concessions(extracted) -> None:
    rr, truth, _ = extracted["deal_004"]
    noted = [(g, w) for g, w in zip(rr.units, truth.units, strict=True) if w.concession_note]
    assert noted, "deal_004 plants hidden concessions"
    for got, want in noted:
        assert len(got.concessions) == 1
        c = got.concessions[0]
        assert c.grounded and c.raw_text == want.concession_note
        if c.kind == "monthly_discount":
            assert c.monthly_equivalent == c.monthly_discount
        else:
            assert c.monthly_equivalent == to_cents(got.in_place_rent * c.months_free / 12)
    clean = [g for g, w in zip(rr.units, truth.units, strict=True) if not w.concession_note]
    assert all(not g.concessions for g in clean)


def test_duplicate_unit_row_is_extracted_twice(extracted) -> None:
    rr, _, _ = extracted["deal_005"]
    ids = [u.unit_id for u in rr.units]
    (dup,) = {i for i in ids if ids.count(i) > 1}
    first, second = [u for u in rr.units if u.unit_id == dup]
    assert first.citations["unit_id"].page + 1 <= second.citations["unit_id"].page


def test_grand_total_matches_the_sum_of_rows_on_a_clean_deal(extracted) -> None:
    rr, _, _ = extracted["deal_001"]
    assert (
        sum((u.in_place_rent or D(0) for u in rr.units), D(0)) == rr.stated_totals["in_place_rent"]
    )


def test_expired_lease_dates_are_extracted_as_printed(extracted) -> None:
    rr, truth, _ = extracted["deal_004"]
    (planted,) = [e for e in truth.expected if e.trap_id == "TRAP_RR_EXPIRED_LEASE"]
    by_id = {u.unit_id: u for u in rr.units}
    for unit_id in planted.unit_ids:
        assert by_id[unit_id].lease_end < truth.as_of_date
