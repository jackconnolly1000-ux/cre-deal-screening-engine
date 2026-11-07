from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.t12_spike import check
from tests.builders import context, line, roll, t12, tweak

D = Decimal


def with_month(value: str, base: str = "1000.00", index: int = 5) -> list[str]:
    months = [base] * 12
    months[index] = value
    return months


def run(*lines, thresholds=None):
    return check(context(roll([]), t12=t12(list(lines)), thresholds=thresholds))


def test_steady_line_passes() -> None:
    assert run(line("R&M", "REPAIRS_MAINTENANCE", "1000.00")) == []


def test_a_five_times_month_is_flagged_once_with_its_month() -> None:
    (f,) = run(line("R&M", "REPAIRS_MAINTENANCE", with_month("5000.00")))
    assert f.check_id == "T12_SPIKE" and f.severity == "warn"
    assert f.line_labels == ["R&M"]
    assert f.details["months"] == "M6" and f.details["median"] == "1000.00"
    assert f.delta == D("4000.00")


def test_exactly_three_times_the_median_passes_and_a_cent_more_fires() -> None:
    assert run(line("R&M", "REPAIRS_MAINTENANCE", with_month("3000.00"))) == []
    assert len(run(line("R&M", "REPAIRS_MAINTENANCE", with_month("3000.01")))) == 1


def test_exactly_the_minimum_dollar_excess_passes_and_a_cent_more_fires() -> None:
    base = "400.00"  # 3.5x = 1,400: factor met, excess of exactly the 1,000 minimum
    assert run(line("R&M", "REPAIRS_MAINTENANCE", with_month("1400.00", base))) == []
    assert len(run(line("R&M", "REPAIRS_MAINTENANCE", with_month("1400.01", base)))) == 1


def test_a_big_multiple_of_a_tiny_line_is_not_flagged() -> None:
    assert run(line("Marketing", "MARKETING", with_month("500.00", "50.00"))) == []


def test_one_finding_per_line_lists_every_spike_month() -> None:
    months = ["1000.00"] * 12
    months[1], months[9] = "6000.00", "7000.00"
    (f,) = run(line("R&M", "REPAIRS_MAINTENANCE", months))
    assert f.details["months"] == "M2,M10"


def test_taxes_and_insurance_are_exempt_because_they_are_paid_in_lumps() -> None:
    lumpy = ["0.00"] * 12
    lumpy[3] = lumpy[9] = "50000.00"
    assert (
        run(
            line("Taxes", "REAL_ESTATE_TAXES", lumpy),
            line("Ins", "INSURANCE", with_month("90000.00")),
        )
        == []
    )


def test_median_zero_lines_are_skipped() -> None:
    one_time = ["0.00"] * 12
    one_time[4] = "20000.00"
    assert run(line("Proceeds", "OTHER_INCOME", one_time, nonrecurring=True)) == []


def test_negative_deductions_are_judged_by_absolute_value() -> None:
    months = ["-1000.00"] * 12
    months[2] = "-9000.00"
    (f,) = run(line("Vacancy", "VACANCY_LOSS", months))
    assert f.details["months"] == "M3"


def test_sections_outside_the_checked_list_are_ignored() -> None:
    assert run(line("Reserves", "CAPEX_RESERVES", with_month("90000.00"))) == []
    assert run(line("Debt", "NON_OPERATING", with_month("90000.00"))) == []


def test_factor_comes_from_the_thresholds_file() -> None:
    strict = tweak("t12_spike", factor=D("1.5"))
    assert (
        len(run(line("R&M", "REPAIRS_MAINTENANCE", with_month("2600.00")), thresholds=strict)) == 1
    )


def test_no_t12_gives_no_findings() -> None:
    assert check(context(roll([]), t12=None)) == []
