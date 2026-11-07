from __future__ import annotations

from decimal import Decimal
from typing import get_args

import pytest

from cre_screen.config import load_config
from cre_screen.reconcile import metrics
from cre_screen.schemas.common import COABucket
from tests.builders import line, proforma, roll, t12, unit

D = Decimal
OCC = ["occupied", "notice"]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("101", "101"), (" a101 ", "A101"), ("a  101", "A 101"), ("0101", "0101"), ("b-2", "B-2")],
)
def test_normalize_unit_id_keeps_leading_zeros(raw: str, normalized: str) -> None:
    assert metrics.normalize_unit_id(raw) == normalized


def test_unique_units_keeps_the_first_printed_row() -> None:
    first, second, other = unit("101", "900.00"), unit("101", "999.00"), unit("102")
    assert metrics.unique_units([first, second, other]) == [first, other]


def test_unique_units_matches_across_case_and_spacing() -> None:
    assert len(metrics.unique_units([unit("a101"), unit(" A101 ")])) == 1
    assert len(metrics.unique_units([unit("0101"), unit("101")])) == 2


def test_units_covered_consumes_one_row_per_listed_id() -> None:
    a1, a2, b = unit("101"), unit("101", "50.00"), unit("102")
    assert metrics.units_covered([a1, b, a2], ["101", "102", "101"]) == [a1, b, a2]
    assert metrics.units_covered([a1, b, a2], ["101"]) == [a1]
    assert metrics.units_covered([a1], ["999"]) == []


def test_rr_inplace_monthly_counts_occupied_like_rows_once() -> None:
    units = [
        unit("101", "1000.00"),
        unit("102", "500.00", status="notice"),
        unit("103", None, status="vacant"),
        unit("104", "700.00", status="model"),
        unit("101", "1000.00"),  # duplicated row
    ]
    assert metrics.rr_inplace_monthly(units, OCC) == D("1500.00")
    assert metrics.rr_inplace_monthly([], OCC) == 0


def test_physical_vacancy_counts_vacant_and_down_over_unique_units() -> None:
    units = [
        unit("1", status="vacant", rent=None),
        unit("2", status="down", rent=None),
        unit("3"),
        unit("4"),
        unit("1", status="vacant", rent=None),
    ]
    assert metrics.physical_vacancy(units, ["vacant", "down"]) == D("0.5")
    assert metrics.physical_vacancy([], ["vacant"]) is None


def test_bucket_section_agrees_with_coa_yaml() -> None:
    coa = load_config().coa.buckets
    for bucket in get_args(COABucket):
        assert metrics.bucket_section(bucket) == coa[bucket].section, bucket


def test_t12_egi_uw_excludes_nonrecurring_and_expenses() -> None:
    statement = t12(
        [
            line("Rent", "GROSS_POTENTIAL_RENT", "1000.00"),
            line("Vacancy", "VACANCY_LOSS", "-100.00"),
            line("Proceeds", "OTHER_INCOME", "5000.00", nonrecurring=True),
            line("Taxes", "REAL_ESTATE_TAXES", "300.00"),
        ]
    )
    assert metrics.t12_egi_uw(statement) == D("10800.00")


def test_pf_revenue_total_sums_revenue_lines_only() -> None:
    pf = proforma(
        {"GROSS_POTENTIAL_RENT": "1000.00", "VACANCY_LOSS": "-100.00", "INSURANCE": "50.00"}
    )
    assert metrics.pf_revenue_total(pf) == D("900.00")


def test_median_of_decimals() -> None:
    assert metrics.median([D("1"), D("3"), D("2")]) == D("2")
    assert metrics.median([D("1"), D("2")]) == D("1.5")


def test_roll_builder_round_trips() -> None:
    assert len(roll([unit("1")]).units) == 1
