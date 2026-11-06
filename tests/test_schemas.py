from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cre_screen.schemas.citation import Citation
from cre_screen.schemas.common import (
    CONTROL_BUCKETS,
    OPEX_BUCKETS,
    RENTAL_BUCKETS,
    REVENUE_BUCKETS,
    to_cents,
)
from cre_screen.schemas.deal import (
    DealPackage,
    DealScore,
    DealTruth,
    TruthT12Line,
    TruthUnit,
)
from cre_screen.schemas.findings import ExpectedFinding, Finding, GateTransition, Override
from cre_screen.schemas.mapping import (
    COAMapping,
    COAMappingBatch,
    ConcessionParse,
    LLMColumnMap,
    LLMHeaderMap,
    VisionRow,
    VisionTablePage,
)
from cre_screen.schemas.proforma import BrokerProForma
from cre_screen.schemas.rent_roll import (
    ColumnMap,
    Concession,
    HeaderMap,
    RentRoll,
    RentRollUnit,
    SubtotalRow,
)
from cre_screen.schemas.t12 import StatedSubtotal, T12Line, T12Statement

D = Decimal
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


# ---------- builders ----------


def pdf_cite(row: int = 3, raw: str = "1,250.00") -> Citation:
    return Citation(
        source_file="rent_roll.pdf",
        page=1,
        row_index=row,
        bbox=(10.0, 20.0, 60.5, 32.25),
        raw_text=raw,
        provenance="text_layer",
    )


def xl_cite() -> Citation:
    return Citation(
        source_file="t12.xlsx",
        sheet="T12",
        cell="F22",
        row_index=21,
        raw_text="(1,000)",
        provenance="excel",
    )


def header_map() -> HeaderMap:
    return HeaderMap(
        columns=[
            ColumnMap(header_text="Unit", maps_to="unit_id"),
            ColumnMap(header_text="Status", maps_to="status"),
            ColumnMap(header_text="Rent", maps_to="in_place_rent"),
        ],
        source="synonym_table",
    )


def unit() -> RentRollUnit:
    return RentRollUnit(
        unit_id="101",
        unit_type="1BR",
        beds=1,
        baths=D("1.5"),
        sq_ft=700,
        status="occupied",
        in_place_rent=D("1250.00"),
        market_rent=D("1300.00"),
        lease_start=date(2026, 1, 1),
        lease_end=date(2026, 12, 31),
        concessions=[concession()],
        audit_flags=["sign_normalized"],
        citations={"unit_id": pdf_cite(), "in_place_rent": pdf_cite()},
    )


def concession() -> Concession:
    return Concession(
        kind="free_months",
        monthly_equivalent=D("104.17"),
        months_free=D("1"),
        lease_term_months=12,
        raw_text="1 mo free",
        grounded=True,
        citation=pdf_cite(),
    )


def rent_roll() -> RentRoll:
    return RentRoll(
        property_name="Erdrick Court",
        as_of_date=date(2026, 10, 31),
        source_file="rent_roll.pdf",
        source_format="pdf_text",
        header_map=header_map(),
        units=[unit()],
        stated_totals={"in_place_rent": D("1250.00")},
        stated_total_citations={"in_place_rent": pdf_cite()},
        subtotals=[
            SubtotalRow(
                label="Page total",
                column="in_place_rent",
                scope="page",
                stated_value=D("1250.00"),
                covers_unit_ids=["101"],
                citation=pdf_cite(),
            )
        ],
        extraction_warnings=["no as-of date found"],
    )


def t12_line(label: str = "Real Estate Taxes") -> T12Line:
    return T12Line(
        label=label,
        bucket="REAL_ESTATE_TAXES",
        monthly=[D("100.00")] * 12,
        stated_total=D("1200.00"),
        mapping_source="synonym_table",
        mapping_confidence=1.0,
        citation=xl_cite(),
        cell_citations={"m01": xl_cite(), "total": xl_cite()},
    )


def t12() -> T12Statement:
    return T12Statement(
        property_name="Erdrick Court",
        period_end=date(2026, 9, 30),
        month_labels=[f"M{i}" for i in range(1, 13)],
        source_file="t12.xlsx",
        source_format="xlsx",
        lines=[t12_line()],
        stated_subtotals=[
            StatedSubtotal(
                label="NOI",
                kind="noi",
                monthly=[D("1.00")] * 11 + [None],
                total=D("11.00"),
                citation=xl_cite(),
            )
        ],
    )


def proforma() -> BrokerProForma:
    return BrokerProForma(
        property_name="Erdrick Court",
        asking_price=D("7500000.00"),
        unit_count=48,
        lines={"GROSS_POTENTIAL_RENT": D("1020000.00"), "VACANCY_LOSS": D("-30600.00")},
        vacancy_rate=D("0.03"),
        mgmt_fee_pct=None,
        source_note="OM p.14",
        citation=Citation(
            source_file="proforma.yaml", row_index=0, raw_text="", provenance="manual_entry"
        ),
    )


def finding() -> Finding:
    return Finding(
        check_id="RR_FOOTING",
        severity="block",
        message="rent roll does not foot",
        delta=D("-12.50"),
        unit_ids=["101"],
        citations=[pdf_cite()],
        details={"cause": "document"},
    )


def package() -> DealPackage:
    return DealPackage(
        deal_id="deal_001",
        run_id="20261106T120000-abcdef12",
        model_id="claude-haiku-5-5",
        rent_roll=rent_roll(),
        t12=t12(),
        proforma=proforma(),
        findings=[finding()],
        gate_log=[GateTransition(from_state="FOOTING", to_state="CHECKS", reason="foots", at=NOW)],
        final_state="REVIEW",
        overrides=[
            Override(
                check_id="RR_FOOTING",
                action="override",
                target="*",
                note="owner confirmed total",
                at=NOW,
            )
        ],
        cost_usd=D("0.0123"),
        wall_seconds=1.5,
        created_at=NOW,
    )


def truth() -> DealTruth:
    return DealTruth(
        deal_id="deal_001",
        seed=7,
        split="dev",
        rr_layout="rr_classic",
        t12_layout="t12_grid",
        rr_format="pdf_text",
        t12_format="pdf_text",
        as_of_date=date(2026, 10, 31),
        units=[
            TruthUnit(
                unit_id="101",
                unit_type="1BR",
                beds=1,
                baths=D("1"),
                sq_ft=700,
                status="occupied",
                in_place_rent=D("1250.00"),
                market_rent=D("1300.00"),
                lease_start=date(2026, 1, 1),
                lease_end=date(2026, 12, 31),
                concession_note="1 mo free",
                page=1,
            )
        ],
        rr_stated_totals={"in_place_rent": D("1250.00")},
        t12_lines=[
            TruthT12Line(
                label="Real Estate Taxes",
                true_bucket="REAL_ESTATE_TAXES",
                monthly=[D("100.00")] * 12,
                stated_total=D("1200.00"),
                is_nonrecurring=False,
            )
        ],
        traps=["TRAP_RR_FOOTING"],
        expected=[
            ExpectedFinding(check_id="RR_FOOTING", trap_id="TRAP_RR_FOOTING", unit_ids=["101"])
        ],
    )


def score() -> DealScore:
    return DealScore(
        deal_id="deal_001",
        split="dev",
        provenance_mix={"text_layer": 40},
        numeric_fields_total=40,
        numeric_fields_correct=39,
        units_in_doc=10,
        units_found=10,
        phantom_units=0,
        coa_dollars_total=D("1000.00"),
        coa_dollars_correct=D("900.00"),
        planted=3,
        caught=2,
        false_positives=1,
        clean_control_findings=0,
        cost_usd=D("0.01"),
        wall_seconds=2.0,
        misses=["TRAP_X: not flagged"],
    )


ROUND_TRIP_CASES: list[Any] = [
    pdf_cite,
    xl_cite,
    concession,
    unit,
    header_map,
    rent_roll,
    t12_line,
    t12,
    proforma,
    finding,
    package,
    truth,
    score,
    lambda: ExpectedFinding(check_id="T12_SPIKE", trap_id="TRAP_SPIKE", line_labels=["Repairs"]),
    lambda: LLMHeaderMap(columns=[LLMColumnMap(header_text="Apt", maps_to="unit_id")]),
    lambda: COAMappingBatch(
        mappings=[
            COAMapping(
                line_label="Water", bucket="UTILITIES", is_nonrecurring=False, rationale="water"
            )
        ]
    ),
    lambda: ConcessionParse(kind="free_months", months_free=D("1.5"), lease_term_months=12),
    lambda: VisionTablePage(
        headers=["Unit", "Rent"],
        rows=[VisionRow(cells={"Unit": "101", "Rent": "$1,250"}, row_kind="unit")],
    ),
]


@pytest.mark.parametrize("build", ROUND_TRIP_CASES)
def test_model_round_trips_through_json(build: Any) -> None:
    original: BaseModel = build()
    restored = type(original).model_validate_json(original.model_dump_json())
    assert restored == original


def test_money_stays_decimal_after_round_trip() -> None:
    restored = RentRollUnit.model_validate_json(unit().model_dump_json())
    assert isinstance(restored.in_place_rent, Decimal)
    assert restored.in_place_rent == D("1250.00")


# ---------- Citation ----------


def test_citation_with_page_is_valid() -> None:
    assert pdf_cite().short() == "rent_roll.pdf p.1 row 3"


def test_citation_with_sheet_and_cell_is_valid() -> None:
    assert xl_cite().short() == "t12.xlsx T12!F22"


def test_citation_manual_entry_needs_no_location() -> None:
    c = Citation(source_file="proforma.yaml", row_index=0, raw_text="", provenance="manual_entry")
    assert c.short() == "proforma.yaml (manual entry)"


def test_citation_without_page_or_cell_fails() -> None:
    with pytest.raises(ValidationError, match="page or an Excel cell"):
        Citation(source_file="a.pdf", row_index=0, raw_text="x", provenance="text_layer")


def test_citation_cell_without_sheet_fails() -> None:
    with pytest.raises(ValidationError, match="sheet name"):
        Citation(source_file="a.xlsx", cell="A1", row_index=0, raw_text="x", provenance="excel")


@pytest.mark.parametrize("cell", ["a1", "A0", "1A", "AAAA1", "A-1"])
def test_citation_rejects_bad_cell_address(cell: str) -> None:
    with pytest.raises(ValidationError):
        Citation(
            source_file="a.xlsx",
            sheet="S",
            cell=cell,
            row_index=0,
            raw_text="x",
            provenance="excel",
        )


def test_citation_page_must_be_one_based() -> None:
    with pytest.raises(ValidationError):
        Citation(source_file="a.pdf", page=0, row_index=0, raw_text="x", provenance="text_layer")


def test_citation_is_frozen() -> None:
    with pytest.raises(ValidationError):
        pdf_cite().row_index = 9  # type: ignore[misc]


def test_citation_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Citation(
            source_file="a.pdf",
            page=1,
            row_index=0,
            raw_text="x",
            provenance="text_layer",
            colour="red",  # type: ignore[call-arg]
        )


# ---------- HeaderMap one-to-one ----------


def cols(*pairs: tuple[str, str]) -> list[ColumnMap]:
    return [ColumnMap(header_text=h, maps_to=m) for h, m in pairs]  # type: ignore[arg-type]


def test_header_map_duplicate_mapping_fails() -> None:
    with pytest.raises(ValidationError, match="mapped more than once"):
        HeaderMap(
            columns=cols(
                ("Unit", "unit_id"),
                ("Apt", "unit_id"),
                ("Rent", "in_place_rent"),
                ("Status", "status"),
            ),
            source="llm",
        )


def test_header_map_missing_required_fails() -> None:
    with pytest.raises(ValidationError, match="missing required"):
        HeaderMap(columns=cols(("Unit", "unit_id"), ("Status", "status")), source="llm")


def test_header_map_needs_status_or_tenant() -> None:
    with pytest.raises(ValidationError, match="'status' or 'tenant_name'"):
        HeaderMap(columns=cols(("Unit", "unit_id"), ("Rent", "in_place_rent")), source="llm")


def test_header_map_tenant_name_can_replace_status() -> None:
    hm = HeaderMap(
        columns=cols(("Unit", "unit_id"), ("Rent", "in_place_rent"), ("Tenant", "tenant_name")),
        source="llm",
    )
    assert len(hm.columns) == 3


def test_header_map_ignore_may_repeat() -> None:
    hm = HeaderMap(
        columns=cols(
            ("Unit", "unit_id"),
            ("Rent", "in_place_rent"),
            ("Status", "status"),
            ("Foo", "ignore"),
            ("Bar", "ignore"),
        ),
        source="llm",
    )
    assert len(hm.columns) == 5


def test_header_map_reports_every_problem_at_once() -> None:
    with pytest.raises(ValidationError) as info:
        HeaderMap(columns=cols(("A", "beds"), ("B", "beds")), source="llm")
    message = str(info.value)
    assert "mapped more than once" in message
    assert "missing required" in message
    assert "'status' or 'tenant_name'" in message


# ---------- T12Line ----------


@pytest.mark.parametrize("n", [0, 11, 13])
def test_t12_line_requires_exactly_12_months(n: int) -> None:
    with pytest.raises(ValidationError):
        T12Line(
            label="x",
            bucket="UTILITIES",
            monthly=[D("1.00")] * n,
            mapping_source="llm",
            mapping_confidence=0.7,
            citation=xl_cite(),
        )


def test_t12_line_confidence_bounded() -> None:
    with pytest.raises(ValidationError):
        T12Line(
            label="x",
            bucket="UTILITIES",
            monthly=[D("1.00")] * 12,
            mapping_source="llm",
            mapping_confidence=1.5,
            citation=xl_cite(),
        )


def test_t12_line_math_helpers() -> None:
    line = t12_line()
    line.monthly[9:12] = [D("10.00"), D("20.00"), D("30.00")]
    assert line.computed_total() == D("960.00")
    assert line.t3_annualized() == D("240.00")


@pytest.mark.parametrize(
    ("bucket", "nonrecurring", "expected"),
    [
        ("UTILITIES", False, True),
        ("UTILITIES", True, False),
        ("NON_OPERATING", False, False),
        ("UNMAPPED", False, False),
    ],
)
def test_t12_line_include_in_uw(bucket: str, nonrecurring: bool, expected: bool) -> None:
    line = t12_line().model_copy(update={"bucket": bucket, "is_nonrecurring": nonrecurring})
    assert line.include_in_uw is expected


def test_t12_line_json_round_trip_does_not_serialize_include_in_uw() -> None:
    assert "include_in_uw" not in t12_line().model_dump()


def test_t12_duplicate_labels_rejected() -> None:
    data = t12().model_dump()
    data["lines"] = [t12_line(), t12_line()]
    with pytest.raises(ValidationError, match="duplicate T-12 line labels"):
        T12Statement(**data)


def test_t12_bucket_totals_respect_uw_only() -> None:
    stmt = t12()
    extra = t12_line("Insurance Proceeds").model_copy(update={"is_nonrecurring": True})
    stmt = stmt.model_copy(update={"lines": [*stmt.lines, extra]})
    assert stmt.bucket_total("REAL_ESTATE_TAXES") == D("2400.00")
    assert stmt.bucket_total("REAL_ESTATE_TAXES", uw_only=True) == D("1200.00")
    assert stmt.bucket_t3_annualized("REAL_ESTATE_TAXES", uw_only=True) == D("1200.00")


# ---------- BrokerProForma ----------


@pytest.mark.parametrize("bucket", ["NON_OPERATING", "UNMAPPED"])
def test_proforma_rejects_control_buckets(bucket: str) -> None:
    base = proforma().model_dump()
    base["lines"] = {bucket: D("1.00")}
    with pytest.raises(ValidationError, match="control buckets"):
        BrokerProForma(**base)


def test_proforma_allows_capex_reserves() -> None:
    base = proforma().model_dump()
    base["lines"] = {"CAPEX_RESERVES": D("12000.00")}
    assert BrokerProForma(**base).lines["CAPEX_RESERVES"] == D("12000.00")


def test_proforma_rate_must_be_between_0_and_1() -> None:
    base = proforma().model_dump()
    base["vacancy_rate"] = D("1.01")
    with pytest.raises(ValidationError):
        BrokerProForma(**base)


# ---------- Money ----------


def test_money_rejects_three_decimal_places() -> None:
    with pytest.raises(ValidationError):
        Concession(
            kind="monthly_discount",
            monthly_equivalent=D("104.171"),
            raw_text="x",
            grounded=True,
            citation=pdf_cite(),
        )


def test_money_rejects_three_decimal_places_in_unit_rent() -> None:
    data = unit().model_dump()
    data["in_place_rent"] = D("1250.005")
    with pytest.raises(ValidationError):
        RentRollUnit(**data)


def test_money_rejects_too_many_digits() -> None:
    data = unit().model_dump()
    data["in_place_rent"] = D("1234567890123.00")
    with pytest.raises(ValidationError):
        RentRollUnit(**data)


def test_money_accepts_two_decimal_places() -> None:
    data = unit().model_dump()
    data["in_place_rent"] = D("1250.05")
    assert RentRollUnit(**data).in_place_rent == D("1250.05")


def test_to_cents_rounds_half_up() -> None:
    assert to_cents(D("1.005")) == D("1.01")
    assert to_cents(D("1.004")) == D("1.00")
    assert to_cents(D("-1.005")) == D("-1.01")


# ---------- other schema rules ----------


def test_zero_rent_occupied_unit_loads() -> None:
    data = unit().model_dump()
    data.update(in_place_rent=D("0.00"), status="occupied")
    assert RentRollUnit(**data).in_place_rent == D("0.00")


def test_expired_lease_unit_loads() -> None:
    data = unit().model_dump()
    data.update(lease_start=date(2020, 1, 1), lease_end=date(2020, 12, 31))
    assert RentRollUnit(**data).lease_end == date(2020, 12, 31)


def test_unit_lease_end_before_start_fails() -> None:
    data = unit().model_dump()
    data.update(lease_start=date(2026, 6, 1), lease_end=date(2026, 5, 1))
    with pytest.raises(ValidationError, match="lease_end before lease_start"):
        RentRollUnit(**data)


def test_unit_needs_unit_id_citation() -> None:
    data = unit().model_dump()
    data["citations"] = {"in_place_rent": pdf_cite()}
    with pytest.raises(ValidationError, match="missing unit_id citation"):
        RentRollUnit(**data)


def test_rent_roll_stated_total_needs_citation() -> None:
    data = rent_roll().model_dump()
    data["stated_total_citations"] = {}
    with pytest.raises(ValidationError, match="every stated total needs a citation"):
        RentRoll(**data)


def test_override_note_min_length() -> None:
    with pytest.raises(ValidationError):
        Override(check_id="RR_FOOTING", action="clear", target="*", note="too short", at=NOW)


def test_extra_fields_forbidden_on_package() -> None:
    data = package().model_dump()
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        DealPackage(**data)


def test_package_open_blocks_filters_by_status_and_severity() -> None:
    blocked = finding()
    cleared = finding().model_copy(update={"status": "cleared"})
    warned = finding().model_copy(update={"severity": "warn"})
    pkg = package().model_copy(update={"findings": [blocked, cleared, warned]})
    assert pkg.open_blocks() == [blocked]


# ---------- bucket tuples ----------


def test_bucket_tuple_sizes() -> None:
    assert len(REVENUE_BUCKETS) == 7
    assert len(OPEX_BUCKETS) == 10
    assert len(CONTROL_BUCKETS) == 3


def test_bucket_tuple_contents() -> None:
    assert REVENUE_BUCKETS[0] == "GROSS_POTENTIAL_RENT"
    assert REVENUE_BUCKETS[-1] == "OTHER_INCOME"
    assert OPEX_BUCKETS[0] == "REAL_ESTATE_TAXES"
    assert OPEX_BUCKETS[-1] == "MARKETING"
    assert CONTROL_BUCKETS == ("CAPEX_RESERVES", "NON_OPERATING", "UNMAPPED")
    assert set(RENTAL_BUCKETS) <= set(REVENUE_BUCKETS)
