from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.pf_mgmt_fee import check
from tests.builders import context, line, proforma, roll, t12, tweak

D = Decimal

# T-12 EGI (underwritten) = 12 x 10,000 = 120,000; management fee 12 x 400 = 4,800 = 4.0%.
LINES = [
    line("Potential Rent", "GROSS_POTENTIAL_RENT", "10000.00"),
    line("Property Management", "MANAGEMENT_FEE", "400.00"),
]


def run(*, pct: str | None = None, lines=None, statement="default", with_pf=True, thresholds=None):
    pf_lines = lines or {"GROSS_POTENTIAL_RENT": "100000.00"}
    pf = proforma(pf_lines, mgmt_fee_pct=pct) if with_pf else None
    stmt = t12(LINES) if statement == "default" else statement
    return check(context(roll([]), t12=stmt, pf=pf, thresholds=thresholds))


def test_pro_forma_at_or_above_the_t12_rate_passes() -> None:
    assert run(pct="0.04") == []
    assert run(pct="0.05") == []


def test_a_two_percent_fee_against_four_percent_actual_warns() -> None:
    (f,) = run(pct="0.02")
    assert f.check_id == "PF_MGMT_FEE" and f.severity == "warn"
    assert f.details == {"pro_forma": "0.0200", "t12": "0.0400"}
    assert "2.0% of revenue" in f.message and "paid 4.0%" in f.message


def test_exactly_half_a_point_below_passes_and_a_hair_lower_warns() -> None:
    assert run(pct="0.035") == []
    assert len(run(pct="0.034999")) == 1


def test_rate_is_derived_from_pro_forma_lines_when_not_stated() -> None:
    lines = {"GROSS_POTENTIAL_RENT": "100000.00", "MANAGEMENT_FEE": "2000.00"}
    (f,) = run(lines=lines)
    assert f.details["pro_forma"] == "0.0200"
    lines["MANAGEMENT_FEE"] = "4000.00"
    assert run(lines=lines) == []


def test_pro_forma_revenue_is_net_of_deductions() -> None:
    lines = {
        "GROSS_POTENTIAL_RENT": "110000.00",
        "VACANCY_LOSS": "-10000.00",
        "MANAGEMENT_FEE": "4000.00",
    }
    assert run(lines=lines) == []  # 4,000 / 100,000 = 4.0%


def test_one_time_income_does_not_dilute_the_t12_rate() -> None:
    proceeds = ["0.00"] * 11 + ["500000.00"]
    statement = t12([*LINES, line("Proceeds", "OTHER_INCOME", proceeds, nonrecurring=True)])
    (f,) = run(pct="0.02", statement=statement)
    assert f.details["t12"] == "0.0400"


def test_margin_comes_from_the_thresholds_file() -> None:
    wide = tweak("pf_mgmt_fee", margin=D("0.03"))
    assert run(pct="0.02", thresholds=wide) == []


def test_no_rate_and_no_fee_line_is_skipped() -> None:
    assert run(lines={"GROSS_POTENTIAL_RENT": "100000.00"}) == []


def test_no_proforma_gives_no_findings() -> None:
    assert run(pct="0.01", with_pf=False) == []


def test_no_t12_gives_no_findings() -> None:
    assert run(pct="0.01", statement=None) == []


def test_t12_without_income_gives_no_findings() -> None:
    assert run(pct="0.01", statement=t12([line("Taxes", "REAL_ESTATE_TAXES")])) == []
