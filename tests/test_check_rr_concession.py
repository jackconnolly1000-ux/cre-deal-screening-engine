from __future__ import annotations

from decimal import Decimal

from cre_screen.reconcile.checks.rr_concession import check
from tests.builders import concession, context, roll, unit

D = Decimal


def rent_roll_with(concession_monthly: str, *, grounded: bool = True):
    """Ten occupied units at $1,000 (total $10,000 a month); unit 1 carries the concession."""
    units = [unit(str(i), "1000.00") for i in range(2, 11)]
    units.insert(
        0, unit("1", "1000.00", concessions=[concession(concession_monthly, grounded=grounded)])
    )
    return roll(units)


def run(rr):
    return check(context(rr))


def test_no_concessions_gives_no_findings() -> None:
    assert run(roll([unit("1"), unit("2")])) == []


def test_small_drag_is_an_info_finding() -> None:
    (f,) = run(rent_roll_with("100.00"))
    assert f.check_id == "RR_CONCESSION" and f.severity == "info"
    assert f.unit_ids == ["1"] and f.delta == D("100.00")
    assert f.details["drag"] == "0.0100"


def test_drag_exactly_at_the_threshold_stays_info() -> None:
    (f,) = run(rent_roll_with("200.00"))  # 200 / 10,000 = 0.02, the configured threshold
    assert f.severity == "info" and f.details["drag"] == "0.0200"


def test_drag_one_cent_over_the_threshold_warns() -> None:
    (f,) = run(rent_roll_with("200.01"))
    assert f.severity == "warn"


def test_ungrounded_concession_warns_even_when_the_drag_is_zero() -> None:
    (f,) = run(rent_roll_with("0.00", grounded=False))
    assert f.severity == "warn"
    assert f.details["ungrounded_units"] == "1"
    assert "need review" in f.message


def test_unit_flagged_ungrounded_without_a_concession_record_is_included() -> None:
    rr = roll([unit("1", flags=["concession_ungrounded"]), unit("2")])
    (f,) = run(rr)
    assert f.severity == "warn" and f.unit_ids == ["1"]


def test_several_units_add_up_into_one_finding() -> None:
    units = [unit(str(i), "1000.00", concessions=[concession("100.00")]) for i in range(1, 4)]
    units += [unit(str(i), "1000.00") for i in range(4, 11)]
    (f,) = run(roll(units))
    assert f.unit_ids == ["1", "2", "3"] and f.delta == D("300.00")
    assert f.details["drag"] == "0.0300" and f.severity == "warn"


def test_zero_rent_roll_does_not_divide_by_zero() -> None:
    rr = roll([unit("1", "0.00", concessions=[concession("50.00")])])
    (f,) = run(rr)
    assert f.details["drag"] == "0.0000" and f.severity == "info"


def test_empty_rent_roll_gives_no_findings() -> None:
    assert run(roll([])) == []
