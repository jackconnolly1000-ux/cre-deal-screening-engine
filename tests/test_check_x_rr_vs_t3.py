from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.x_rr_vs_t3 import check
from tests.builders import context, line, roll, t12, tweak, unit

D = Decimal

# GPR of 10,000 a month: last three months annualize to 3 x 10,000 x 4 = 120,000.
T12 = t12([line("Potential Rent", "GROSS_POTENTIAL_RENT", "10000.00")])


def rent_roll(per_unit: str, n: int = 10):
    return roll([unit(str(i), per_unit) for i in range(n)])


def run(rr, statement=T12, thresholds=None):
    return check(context(rr, t12=statement, thresholds=thresholds))


def test_matching_rents_pass() -> None:
    assert run(rent_roll("1000.00")) == []


def test_gap_of_exactly_five_percent_passes() -> None:
    assert run(rent_roll("1050.00")) == []  # 10 x 1,050 x 12 = 126,000 = 120,000 x 1.05
    assert run(rent_roll("950.00")) == []  # 114,000 = 120,000 x 0.95


def test_gap_a_cent_per_unit_over_five_percent_warns() -> None:
    (f,) = run(rent_roll("1050.01"))
    assert f.check_id == "X_RR_VS_T3" and f.severity == "warn"
    assert f.details["t3_annual"] == "120000.00"
    assert f.details["rr_annual"] == "126001.20"
    assert f.delta == D("6001.20")
    assert "above" in f.message


def test_rents_below_the_t12_also_warn() -> None:
    (f,) = run(rent_roll("900.00"))
    assert f.delta == D("-12000.00") and "below" in f.message
    assert f.details["gap"] == "0.1000"


def test_rental_buckets_net_out_deductions() -> None:
    statement = t12(
        [
            line("Potential Rent", "GROSS_POTENTIAL_RENT", "11000.00"),
            line("Vacancy", "VACANCY_LOSS", "-1000.00"),
            line("Taxes", "REAL_ESTATE_TAXES", "5000.00"),  # not a rental bucket
        ]
    )
    assert run(rent_roll("1000.00"), statement) == []


def test_only_the_last_three_months_are_used() -> None:
    months = ["5000.00"] * 9 + ["10000.00"] * 3
    statement = t12([line("Potential Rent", "GROSS_POTENTIAL_RENT", months)])
    assert run(rent_roll("1000.00"), statement) == []


def test_gap_threshold_comes_from_the_thresholds_file() -> None:
    loose = tweak("x_rr_vs_t3", gap_warn_pct=D("0.20"))
    assert run(rent_roll("1100.00"), thresholds=loose) == []


def test_duplicated_rent_roll_rows_are_not_double_counted() -> None:
    units = [unit(str(i), "1000.00") for i in range(10)] + [unit("3", "1000.00")]
    assert run(roll(units)) == []


def test_only_occupied_like_units_count() -> None:
    units = [unit(str(i), "1000.00") for i in range(10)] + [unit("v", None, status="vacant")]
    assert run(roll(units)) == []


def test_no_t12_gives_no_findings() -> None:
    assert check(context(rent_roll("5000.00"), t12=None)) == []


def test_t12_without_rental_income_gives_no_findings() -> None:
    empty = t12([line("Taxes", "REAL_ESTATE_TAXES", "100.00")])
    assert run(rent_roll("5000.00"), empty) == []
