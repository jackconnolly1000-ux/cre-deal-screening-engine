from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.pf_vacancy import check
from tests.builders import context, proforma, roll, tweak, unit

D = Decimal


def rent_roll(vacant: int, total: int = 100):
    units = [unit(str(i), None, status="vacant") for i in range(vacant)]
    units += [unit(str(i), "1000.00") for i in range(vacant, total)]
    return roll(units)


def run(rr, *, rate: str | None = None, lines=None, with_pf=True, thresholds=None):
    pf = proforma(lines or {"GROSS_POTENTIAL_RENT": "1000000.00"}, vacancy_rate=rate)
    return check(context(rr, pf=pf if with_pf else None, thresholds=thresholds))


def test_pro_forma_vacancy_at_or_above_actual_passes() -> None:
    assert run(rent_roll(8), rate="0.08") == []
    assert run(rent_roll(8), rate="0.12") == []


def test_a_much_lower_assumption_warns_with_both_rates() -> None:
    (f,) = run(rent_roll(8), rate="0.03")
    assert f.check_id == "PF_VACANCY" and f.severity == "warn"
    assert f.details == {"pro_forma": "0.0300", "physical": "0.0800"}
    assert "3.0% vacancy" in f.message and "8.0% physical" in f.message


def test_exactly_half_a_point_below_actual_passes_and_a_hair_lower_warns() -> None:
    assert run(rent_roll(8), rate="0.075") == []
    assert len(run(rent_roll(8), rate="0.074999")) == 1


def test_rate_is_derived_from_pro_forma_lines_when_not_stated() -> None:
    lines = {"GROSS_POTENTIAL_RENT": "1000000.00", "VACANCY_LOSS": "-30000.00"}
    (f,) = run(rent_roll(8), lines=lines)
    assert f.details["pro_forma"] == "0.0300"
    lines["VACANCY_LOSS"] = "-80000.00"
    assert run(rent_roll(8), lines=lines) == []


def test_stated_rate_wins_over_the_lines() -> None:
    lines = {"GROSS_POTENTIAL_RENT": "1000000.00", "VACANCY_LOSS": "-80000.00"}
    assert len(run(rent_roll(8), rate="0.02", lines=lines)) == 1


def test_no_rate_and_no_lines_is_skipped() -> None:
    assert run(rent_roll(8), lines={"INSURANCE": "100.00"}) == []
    assert run(rent_roll(8), lines={"VACANCY_LOSS": "-100.00"}) == []


def test_down_units_count_as_vacant() -> None:
    units = [unit(str(i), None, status="down") for i in range(8)]
    units += [unit(str(i), "1000.00") for i in range(8, 100)]
    assert len(run(roll(units), rate="0.01")) == 1


def test_a_duplicated_vacant_row_does_not_inflate_physical_vacancy() -> None:
    rr = rent_roll(5)
    rr.units.append(rr.units[0].model_copy(deep=True))  # the same vacant unit printed twice
    assert run(rr, rate="0.05") == []


def test_margin_comes_from_the_thresholds_file() -> None:
    wide = tweak("pf_vacancy", margin=D("0.05"))
    assert run(rent_roll(8), rate="0.03", thresholds=wide) == []


def test_no_proforma_gives_no_findings() -> None:
    assert run(rent_roll(8), with_pf=False) == []


def test_empty_rent_roll_gives_no_findings() -> None:
    assert run(roll([]), rate="0.01") == []
