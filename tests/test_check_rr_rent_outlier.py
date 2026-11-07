from __future__ import annotations

from cre_screen.reconcile.checks.rr_rent_outlier import check
from tests.builders import context, roll, tweak, unit


def run(*units, thresholds=None):
    return check(context(roll(list(units)), thresholds=thresholds))


def group(*rents: str, unit_type: str = "1x1", start: int = 1):
    return [unit(f"{unit_type}-{start + i}", r, unit_type=unit_type) for i, r in enumerate(rents)]


def test_uniform_rents_pass() -> None:
    assert run(*group("1000.00", "1010.00", "990.00", "1005.00")) == []


def test_one_far_unit_is_flagged_with_the_group_median() -> None:
    (f,) = run(*group("1000.00", "1000.00", "1000.00", "1600.00"))
    assert f.check_id == "RR_RENT_OUTLIER" and f.severity == "warn"
    assert f.unit_ids == ["1x1-4"]
    assert f.details == {"group": "1x1", "median": "1000.00", "group_size": "4"}


def test_exactly_25_percent_from_the_median_passes_and_a_cent_more_fires() -> None:
    assert run(*group("1000.00", "1000.00", "1250.00")) == []
    (f,) = run(*group("1000.00", "1000.00", "1250.01"))
    assert f.unit_ids == ["1x1-3"]


def test_low_outliers_are_flagged_too() -> None:
    (f,) = run(*group("1000.00", "1000.00", "1000.00", "700.00"))
    assert f.unit_ids == ["1x1-4"]


def test_a_group_of_two_is_too_small_to_judge_and_three_is_enough() -> None:
    assert run(*group("1000.00", "2000.00")) == []
    assert len(run(*group("1000.00", "1000.00", "2000.00"))) == 1


def test_min_group_size_comes_from_the_thresholds_file() -> None:
    strict = tweak("rr_rent_outlier", min_group_size=5)
    assert run(*group("1000.00", "1000.00", "1000.00", "2000.00"), thresholds=strict) == []


def test_zero_and_missing_rents_do_not_count_toward_group_size_or_median() -> None:
    units = [
        *group("1000.00", "1000.00"),
        unit("v1", None, status="vacant"),
        unit("z1", "0.00"),
    ]
    assert run(*units) == []


def test_groups_are_judged_separately_one_finding_each() -> None:
    findings = run(
        *group("1000.00", "1000.00", "1000.00", "2000.00", unit_type="1x1"),
        *group("1500.00", "1500.00", "1500.00", "3000.00", unit_type="2x2"),
    )
    assert sorted(f.details["group"] for f in findings) == ["1x1", "2x2"]


def test_units_without_a_type_fall_back_to_beds_and_baths() -> None:
    units = [
        unit(str(i), r, unit_type=None, beds=2, baths="1.0")
        for i, r in enumerate(["1000.00", "1000.00", "1000.00", "2000.00"])
    ]
    (f,) = run(*units)
    assert f.details["group"] == "2x1"


def test_units_with_no_type_and_no_beds_are_skipped() -> None:
    units = [unit(str(i), "1000.00", unit_type=None) for i in range(3)]
    units.append(unit("x", "9000.00", unit_type=None))
    assert run(*units) == []


def test_empty_rent_roll_gives_no_findings() -> None:
    assert run() == []
