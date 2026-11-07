from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.t12_nonrecurring import check
from tests.builders import context, line, roll, t12

D = Decimal


def one_month(value: str, index: int = 3) -> list[str]:
    months = ["0.00"] * 12
    months[index] = value
    return months


def run(*lines):
    return check(context(roll([]), t12=t12(list(lines))))


def test_recurring_lines_pass() -> None:
    assert (
        run(line("Late Fees", "OTHER_INCOME", "100.00"), line("Taxes", "REAL_ESTATE_TAXES")) == []
    )


def test_a_one_time_line_is_a_warning_naming_its_label_and_amount() -> None:
    (f,) = run(
        line("Late Fees", "OTHER_INCOME", "100.00"),
        line("Insurance Proceeds", "OTHER_INCOME", one_month("15000.00"), nonrecurring=True),
    )
    assert f.check_id == "T12_NONRECURRING" and f.severity == "warn"
    assert f.line_labels == ["Insurance Proceeds"]
    assert f.details == {"Insurance Proceeds": "15000.00"}
    assert "excluded from underwritten NOI" in f.message


def test_a_single_flagged_line_is_enough_to_fire() -> None:
    assert len(run(line("Refund", "OTHER_INCOME", one_month("1.00"), nonrecurring=True))) == 1


def test_several_one_time_lines_are_one_finding() -> None:
    (f,) = run(
        line("Insurance Proceeds", "OTHER_INCOME", one_month("100.00"), nonrecurring=True),
        line("Lease Buyout Fee", "OTHER_INCOME", one_month("200.00", 7), nonrecurring=True),
    )
    assert f.line_labels == ["Insurance Proceeds", "Lease Buyout Fee"]
    assert f.details["Lease Buyout Fee"] == "200.00"


def test_empty_t12_gives_no_findings() -> None:
    assert run() == []


def test_no_t12_gives_no_findings() -> None:
    assert check(context(roll([]), t12=None)) == []
