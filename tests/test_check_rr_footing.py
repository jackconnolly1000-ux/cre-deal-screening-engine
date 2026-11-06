from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cre_screen.config import ThresholdsConfig, load_config
from cre_screen.reconcile.checks.rr_footing import check
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.rent_roll import (
    ColumnMap,
    HeaderMap,
    RentRoll,
    RentRollUnit,
    SubtotalRow,
)

D = Decimal
THRESHOLDS = load_config().thresholds


def cite(row: int = 0) -> Citation:
    return Citation(
        source_file="rent_roll.pdf", page=1, row_index=row, raw_text="x", provenance="text_layer"
    )


def unit(unit_id: str, rent: str | None, status: str = "occupied", market: str | None = None):
    return RentRollUnit(
        unit_id=unit_id,
        status=status,  # type: ignore[arg-type]
        in_place_rent=None if rent is None else D(rent),
        market_rent=None if market is None else D(market),
        citations={"unit_id": cite()},
    )


def roll(
    units: list[RentRollUnit],
    in_place: str | None = None,
    market: str | None = None,
    subtotals: list[SubtotalRow] | None = None,
) -> RentRoll:
    totals = {}
    cites = {}
    if in_place is not None:
        totals["in_place_rent"] = D(in_place)
        cites["in_place_rent"] = cite(99)
    if market is not None:
        totals["market_rent"] = D(market)
        cites["market_rent"] = cite(99)
    return RentRoll(
        source_file="rent_roll.pdf",
        source_format="pdf_text",
        header_map=HeaderMap(
            columns=[
                ColumnMap(header_text="Unit", maps_to="unit_id"),
                ColumnMap(header_text="Status", maps_to="status"),
                ColumnMap(header_text="Rent", maps_to="in_place_rent"),
            ],
            source="synonym_table",
        ),
        units=units,
        stated_totals=totals,
        stated_total_citations=cites,
        subtotals=subtotals or [],
    )


def run(rr: RentRoll, thresholds: ThresholdsConfig = THRESHOLDS):
    return check(
        CheckContext(
            rent_roll=rr, t12=None, proforma=None, thresholds=thresholds, as_of=date(2026, 9, 30)
        )
    )


UNITS = [unit("101", "1000.00"), unit("102", "1500.50"), unit("103", None, "vacant")]


def test_exact_total_passes_with_no_findings() -> None:
    assert run(roll(UNITS, in_place="2500.50")) == []


def test_footing_passes_exactly_at_the_one_dollar_tolerance() -> None:
    assert run(roll(UNITS, in_place="2501.50")) == []
    assert run(roll(UNITS, in_place="2499.50")) == []


def test_footing_fails_one_cent_over_tolerance() -> None:
    (f,) = run(roll(UNITS, in_place="2501.51"))
    assert f.check_id == "RR_FOOTING"
    assert f.severity == "block"
    assert f.delta == D("-1.01")
    assert f.details["column"] == "in_place_rent"
    assert f.details["stated"] == "2501.51"


def test_digit_transposition_is_a_blocking_failure() -> None:
    (f,) = run(roll(UNITS, in_place="5200.50"))
    assert f.severity == "block"
    assert f.delta == D("-2700.00")
    assert f.citations == [cite(99)]


def test_occupied_only_basis_passes() -> None:
    units = [*UNITS, unit("104", "400.00", status="down")]  # a down unit that still shows rent
    rr = roll(units, in_place="2500.50")  # stated total excludes the down unit
    assert run(rr) == []


def test_basis_is_recorded_when_it_fails() -> None:
    units = [*UNITS, unit("104", "400.00", status="down")]
    (f,) = run(roll(units, in_place="9999.00"))
    assert f.details["basis"] in {"all_units", "occupied_like"}
    assert f.details["computed_all_units"] == "2900.50"
    assert f.details["computed_occupied_like"] == "2500.50"


def test_missing_stated_total_gives_a_single_info_finding() -> None:
    (f,) = run(roll(UNITS))
    assert f.severity == "info"
    assert "not verifiable" in f.message


def test_market_only_total_does_not_make_in_place_verifiable() -> None:
    findings = run(roll(UNITS, market="0.00"))
    assert [f.severity for f in findings] == ["info"]


def test_market_rent_failure_uses_the_configured_severity() -> None:
    units = [unit("101", "1000.00", market="1100.00"), unit("102", "900.00", market="1000.00")]
    findings = run(roll(units, in_place="1900.00", market="2000.00"))
    assert [(f.details["column"], f.severity) for f in findings] == [("market_rent", "warn")]
    block = THRESHOLDS.model_copy(
        update={
            "rr_footing": THRESHOLDS.rr_footing.model_copy(update={"market_rent_severity": "block"})
        }
    )
    findings = run(roll(units, in_place="1900.00", market="2000.00"), block)
    assert findings[0].severity == "block"


def test_tolerance_comes_from_the_thresholds_file() -> None:
    loose = THRESHOLDS.model_copy(
        update={"rr_footing": THRESHOLDS.rr_footing.model_copy(update={"tolerance_usd": D("5.00")})}
    )
    assert run(roll(UNITS, in_place="2505.50"), loose) == []
    assert len(run(roll(UNITS, in_place="2505.51"), loose)) == 1


def sub(label: str, stated: str, covers: list[str], scope: str = "page") -> SubtotalRow:
    return SubtotalRow(
        label=label,
        column="in_place_rent",
        scope=scope,  # type: ignore[arg-type]
        stated_value=D(stated),
        covers_unit_ids=covers,
        citation=cite(50),
    )


def test_page_subtotal_that_foots_passes() -> None:
    rr = roll(UNITS, in_place="2500.50", subtotals=[sub("Page total", "2500.50", ["101", "102"])])
    assert run(rr) == []


def test_page_subtotal_that_does_not_foot_is_reported_with_its_units() -> None:
    rr = roll(UNITS, in_place="2500.50", subtotals=[sub("Page total", "2400.50", ["101", "102"])])
    (f,) = run(rr)
    assert f.severity == "block"
    assert f.unit_ids == ["101", "102"]
    assert f.delta == D("100.00")
    assert f.details["scope"] == "page"


def test_grand_total_subtotal_row_is_not_double_counted() -> None:
    rr = roll(
        UNITS,
        in_place="2500.50",
        subtotals=[sub("Total", "2500.50", ["101", "102", "103"], scope="grand_total")],
    )
    assert run(rr) == []


def test_subtotal_with_a_duplicated_unit_id_counts_each_printed_row() -> None:
    units = [unit("101", "1000.00"), unit("102", "500.00"), unit("101", "1000.00")]
    rr = roll(
        units,
        in_place="2500.00",
        subtotals=[sub("Page total", "2500.00", ["101", "102", "101"])],
    )
    assert run(rr) == []


def test_subtotal_alone_makes_the_deal_verifiable() -> None:
    rr = roll(UNITS, subtotals=[sub("Page total", "2500.50", ["101", "102"])])
    assert run(rr) == []


@pytest.mark.parametrize("rent", [None, "0.00"])
def test_units_without_rent_count_as_zero(rent: str | None) -> None:
    rr = roll([unit("101", "1000.00"), unit("102", rent)], in_place="1000.00")
    assert run(rr) == []
