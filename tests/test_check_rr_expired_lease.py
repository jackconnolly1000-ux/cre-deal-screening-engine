from __future__ import annotations

from datetime import date, timedelta

from cre_screen.reconcile.checks.rr_expired_lease import check
from tests.builders import AS_OF, context, roll, tweak, unit


def run(*units, thresholds=None):
    return check(context(roll(list(units)), thresholds=thresholds))


def test_current_leases_pass() -> None:
    assert run(unit("1", lease_end=date(2027, 1, 31)), unit("2", lease_end=date(2027, 6, 30))) == []


def test_a_lease_ending_the_day_before_as_of_is_expired() -> None:
    (f,) = run(unit("1", lease_end=AS_OF - timedelta(days=1)))
    assert f.check_id == "RR_EXPIRED_LEASE" and f.severity == "warn"
    assert f.unit_ids == ["1"]


def test_a_lease_ending_on_the_as_of_date_is_the_edge_that_passes() -> None:
    assert run(unit("1", lease_end=AS_OF)) == []


def test_missing_lease_end_is_month_to_month_by_default() -> None:
    (f,) = run(unit("1", lease_end=None))
    assert f.unit_ids == ["1"]


def test_missing_lease_end_can_be_ignored_through_the_thresholds() -> None:
    off = tweak("rr_expired_lease", treat_missing_lease_end_as_mtm=False)
    assert run(unit("1", lease_end=None), thresholds=off) == []


def test_mtm_share_is_flagged_units_over_occupied_like_units() -> None:
    (f,) = run(
        unit("1", lease_end=date(2026, 1, 1)),
        unit("2"),
        unit("3"),
        unit("4"),
        unit("5", None, status="vacant", lease_end=None),
    )
    assert f.details["mtm_share"] == "0.2500"
    assert f.details["occupied_like_count"] == "4"


def test_vacant_units_are_never_flagged() -> None:
    assert run(unit("1", None, status="vacant", lease_end=None)) == []


def test_notice_units_are_checked() -> None:
    (f,) = run(unit("1", status="notice", lease_end=date(2026, 1, 1)))
    assert f.unit_ids == ["1"]


def test_empty_rent_roll_gives_no_findings() -> None:
    assert run() == []
