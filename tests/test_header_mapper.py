from __future__ import annotations

import pytest

from cre_screen.config import load_config
from cre_screen.errors import HeaderMapError
from cre_screen.mapping.header_mapper import map_headers, match_headers, normalize_header
from cre_screen.schemas.mapping import LLMColumnMap, LLMHeaderMap
from tests.fakes import FakeLLMClient

CFG = load_config().rr_headers

GOOD = ["Unit", "Type", "Status", "Contract Rent", "Market Rent", "Lease From", "Lease Exp"]


def fake_for(headers: list[str], mapping: dict[str, str]) -> FakeLLMClient:
    response = LLMHeaderMap(
        columns=[LLMColumnMap(header_text=h, maps_to=mapping[h]) for h in mapping]  # type: ignore[arg-type]
    )
    return FakeLLMClient({("header_map", frozenset(headers)): response})


def test_normalize_header() -> None:
    assert normalize_header("  Unit #  ") == "unit"
    assert normalize_header("Sq. Ft.") == "sq ft"
    assert normalize_header("In-Place Rent") == "in place rent"


def test_synonym_path_makes_zero_llm_calls() -> None:
    fake = FakeLLMClient()
    header_map, warnings = map_headers(GOOD, [], CFG, fake)
    assert fake.calls == []
    assert warnings == []
    assert header_map.source == "synonym_table"
    got = {c.header_text: c.maps_to for c in header_map.columns}
    assert got == {
        "Unit": "unit_id",
        "Type": "unit_type",
        "Status": "status",
        "Contract Rent": "in_place_rent",
        "Market Rent": "market_rent",
        "Lease From": "lease_start",
        "Lease Exp": "lease_end",
    }


def test_exact_match_beats_a_fuzzy_subset_tie() -> None:
    # token_set_ratio("rent", "market rent") is 100, so "Rent" must not be taken as market rent
    got = match_headers(["Unit", "Rent", "Market Rent", "Status"], CFG)
    assert got["Rent"] == "in_place_rent"
    assert got["Market Rent"] == "market_rent"


def test_each_field_is_used_once() -> None:
    got = match_headers(["Unit", "Apt", "Rent", "Status"], CFG)
    assert list(got.values()).count("unit_id") == 1


def test_unmatched_headers_become_ignore_on_the_synonym_path() -> None:
    header_map, _ = map_headers([*GOOD, "Deposit"], [], CFG, FakeLLMClient())
    assert {c.header_text: c.maps_to for c in header_map.columns}["Deposit"] == "ignore"


def test_header_order_is_preserved() -> None:
    header_map, _ = map_headers(GOOD, [], CFG, FakeLLMClient())
    assert [c.header_text for c in header_map.columns] == GOOD


def test_llm_path_is_used_when_required_fields_are_missing() -> None:
    headers = ["Apartment Ref", "Occ", "Monthly Charge", "Bonus"]
    fake = fake_for(
        headers,
        {
            "Apartment Ref": "unit_id",
            "Occ": "status",
            "Monthly Charge": "in_place_rent",
            "Bonus": "ignore",
        },
    )
    header_map, warnings = map_headers(headers, [{"Apartment Ref": "101"}], CFG, fake)
    assert header_map.source == "llm"
    assert warnings == []
    assert len(fake.calls_for("header_map")) == 1
    assert "Apartment Ref" in fake.calls[0].user_content[0]["text"]


def test_llm_path_fills_missing_headers_as_ignore() -> None:
    headers = ["Apartment Ref", "Occ", "Monthly Charge", "Bonus"]
    fake = fake_for(
        headers,
        {"Apartment Ref": "unit_id", "Occ": "status", "Monthly Charge": "in_place_rent"},
    )
    header_map, warnings = map_headers(headers, [], CFG, fake)
    assert {c.header_text: c.maps_to for c in header_map.columns}["Bonus"] == "ignore"
    assert any("omitted" in w and "Bonus" in w for w in warnings)


def test_llm_path_drops_unknown_and_repeated_headers_with_warnings() -> None:
    headers = ["Apartment Ref", "Occ", "Monthly Charge"]
    response = LLMHeaderMap(
        columns=[
            LLMColumnMap(header_text="Apartment Ref", maps_to="unit_id"),
            LLMColumnMap(header_text="Apartment Ref", maps_to="ignore"),
            LLMColumnMap(header_text="Invented", maps_to="beds"),
            LLMColumnMap(header_text="Occ", maps_to="status"),
            LLMColumnMap(header_text="Monthly Charge", maps_to="in_place_rent"),
        ]
    )
    fake = FakeLLMClient({("header_map", frozenset(headers)): response})
    header_map, warnings = map_headers(headers, [], CFG, fake)
    assert [c.header_text for c in header_map.columns] == headers
    assert any("unknown header" in w for w in warnings)
    assert any("twice" in w for w in warnings)


def test_duplicate_mapping_from_the_llm_raises_after_one_retry() -> None:
    headers = ["Apartment Ref", "Occ", "Monthly Charge", "Other Ref"]
    fake = fake_for(
        headers,
        {
            "Apartment Ref": "unit_id",
            "Other Ref": "unit_id",
            "Occ": "status",
            "Monthly Charge": "in_place_rent",
        },
    )
    with pytest.raises(HeaderMapError, match="mapped more than once"):
        map_headers(headers, [], CFG, fake)
    assert len(fake.calls_for("header_map")) == 2
    assert "rejected" in fake.calls[1].user_content[0]["text"]


def test_llm_response_missing_required_field_raises() -> None:
    headers = ["Apartment Ref", "Occ", "Bonus"]
    fake = fake_for(headers, {"Apartment Ref": "unit_id", "Occ": "status", "Bonus": "ignore"})
    with pytest.raises(HeaderMapError, match="missing required"):
        map_headers(headers, [], CFG, fake)


def test_only_the_first_three_sample_rows_reach_the_prompt() -> None:
    headers = ["Apartment Ref", "Occ", "Monthly Charge"]
    fake = fake_for(
        headers, {"Apartment Ref": "unit_id", "Occ": "status", "Monthly Charge": "in_place_rent"}
    )
    rows = [{"Apartment Ref": f"10{i}"} for i in range(6)]
    map_headers(headers, rows, CFG, fake)
    text = fake.calls[0].user_content[0]["text"]
    assert "103" not in text and "102" in text
