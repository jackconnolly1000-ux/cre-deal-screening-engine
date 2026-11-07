from __future__ import annotations

from cre_screen.reconcile.checks.rr_zero_rent_occ import check
from tests.builders import context, roll, tweak, unit


def run(*units, thresholds=None):
    return check(context(roll(list(units)), thresholds=thresholds))


def test_paying_units_pass() -> None:
    assert run(unit("1", "900.00"), unit("2", "1200.00", status="notice")) == []


def test_zero_and_missing_rent_on_occupied_units_warn() -> None:
    (f,) = run(unit("1", "0.00"), unit("2", None), unit("3", "900.00"))
    assert f.check_id == "RR_ZERO_RENT_OCC" and f.severity == "warn"
    assert f.unit_ids == ["1", "2"]
    assert f.details["count"] == "2"


def test_one_cent_of_rent_is_the_edge_that_passes() -> None:
    assert run(unit("1", "0.01")) == []
    assert len(run(unit("1", "0.00"))) == 1


def test_notice_units_count_as_occupied_like() -> None:
    (f,) = run(unit("1", "0.00", status="notice"))
    assert f.unit_ids == ["1"]


def test_vacant_and_down_units_are_not_flagged() -> None:
    assert run(unit("1", None, status="vacant"), unit("2", "0.00", status="down")) == []


def test_model_and_employee_units_give_a_separate_info_finding() -> None:
    findings = run(unit("1", "0.00", status="model"), unit("2", None, status="employee"))
    assert [(f.severity, f.unit_ids) for f in findings] == [("info", ["1", "2"])]


def test_both_findings_can_appear_together() -> None:
    findings = run(unit("1", "0.00"), unit("2", "0.00", status="model"))
    assert [f.severity for f in findings] == ["warn", "info"]
    assert all(f.check_id == "RR_ZERO_RENT_OCC" for f in findings)


def test_occupied_like_statuses_come_from_the_thresholds_file() -> None:
    only_occupied = tweak("rr_zero_rent_occ", occupied_like_statuses=["occupied"])
    assert run(unit("1", "0.00", status="notice"), thresholds=only_occupied) == []


def test_empty_rent_roll_gives_no_findings() -> None:
    assert run() == []
