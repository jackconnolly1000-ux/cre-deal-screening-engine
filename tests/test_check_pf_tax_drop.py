from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.pf_tax_drop import check
from tests.builders import context, line, proforma, roll, t12, tweak

D = Decimal

# T-12 taxes: 12 x 1,000 = 12,000 a year.
T12 = t12([line("Property Taxes", "REAL_ESTATE_TAXES", "1000.00")])


def run(taxes: str | None, statement=T12, thresholds=None, with_pf=True):
    lines = {"GROSS_POTENTIAL_RENT": "100000.00"}
    if taxes is not None:
        lines["REAL_ESTATE_TAXES"] = taxes
    pf = proforma(lines) if with_pf else None
    return check(context(roll([]), t12=statement, pf=pf, thresholds=thresholds))


def test_flat_or_higher_taxes_pass() -> None:
    assert run("12000.00") == []
    assert run("13500.00") == []


def test_any_decrease_warns_at_the_default_zero_threshold() -> None:
    (f,) = run("11999.99")
    assert f.check_id == "PF_TAX_DROP" and f.severity == "warn"
    assert f.delta == D("-0.01")
    assert f.line_labels == ["Property Taxes"]
    assert "reassessment" in f.message and "needs support" in f.message
    assert f.details["pro_forma"] == "11999.99" and f.details["t12"] == "12000.00"


def test_a_material_drop_reports_the_percentage() -> None:
    (f,) = run("9000.00")
    assert f.details["drop"] == "0.2500" and "25.0% below" in f.message


def test_exactly_at_the_allowed_drop_passes_and_a_cent_below_warns() -> None:
    five_percent = tweak("pf_tax_drop", drop_pct=D("0.05"))
    assert run("11400.00", thresholds=five_percent) == []
    assert len(run("11399.99", thresholds=five_percent)) == 1


def test_no_proforma_gives_no_findings() -> None:
    assert run("1.00", with_pf=False) == []


def test_proforma_without_a_tax_line_gives_no_findings() -> None:
    assert run(None) == []


def test_no_t12_gives_no_findings() -> None:
    assert run("1.00", statement=None) == []


def test_t12_with_no_taxes_cannot_be_undercut() -> None:
    assert run("0.00", statement=t12([line("Rent", "GROSS_POTENTIAL_RENT")])) == []
