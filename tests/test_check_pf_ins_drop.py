from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.pf_ins_drop import check
from tests.builders import context, line, proforma, roll, t12, tweak

D = Decimal

# T-12 insurance: 10,000 paid once in the year.
ONE_PAYMENT = ["10000.00"] + ["0.00"] * 11
T12 = t12([line("Liability Insurance", "INSURANCE", ONE_PAYMENT)])


def run(insurance: str | None, statement=T12, thresholds=None, with_pf=True):
    lines = {"GROSS_POTENTIAL_RENT": "100000.00"}
    if insurance is not None:
        lines["INSURANCE"] = insurance
    pf = proforma(lines) if with_pf else None
    return check(context(roll([]), t12=statement, pf=pf, thresholds=thresholds))


def test_flat_or_higher_insurance_passes() -> None:
    assert run("10000.00") == []
    assert run("12000.00") == []


def test_a_small_drop_under_ten_percent_passes() -> None:
    assert run("9500.00") == []


def test_exactly_ten_percent_below_passes_and_a_cent_lower_warns() -> None:
    assert run("9000.00") == []
    (f,) = run("8999.99")
    assert f.check_id == "PF_INS_DROP" and f.severity == "warn"
    assert f.delta == D("-1000.01")
    assert f.line_labels == ["Liability Insurance"]
    assert f.details["drop"] == "0.1000"


def test_a_large_drop_reports_the_percentage() -> None:
    (f,) = run("7000.00")
    assert "30.0% below" in f.message


def test_drop_threshold_comes_from_the_thresholds_file() -> None:
    loose = tweak("pf_ins_drop", drop_pct=D("0.30"))
    assert run("7000.00", thresholds=loose) == []


def test_no_proforma_gives_no_findings() -> None:
    assert run("1.00", with_pf=False) == []


def test_proforma_without_an_insurance_line_gives_no_findings() -> None:
    assert run(None) == []


def test_no_t12_gives_no_findings() -> None:
    assert run("1.00", statement=None) == []
