from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from cre_screen.schemas.deal import DealPackage, DealTruth, TruthT12Line, TruthUnit
from cre_screen.schemas.findings import ExpectedFinding, Finding
from evals.scoring import failed_score, finding_matches, score_deal
from tests.builders import cite, line, roll, t12, unit

D = Decimal
NOW = datetime(2026, 9, 30, tzinfo=UTC)


def truth_unit(unit_id: str, rent: str | None = "1000.00", market: str = "1100.00") -> TruthUnit:
    return TruthUnit(
        unit_id=unit_id,
        unit_type="1x1",
        beds=1,
        baths=D("1"),
        sq_ft=700,
        status="occupied",
        in_place_rent=None if rent is None else D(rent),
        market_rent=D(market),
        lease_start=date(2026, 1, 1),
        lease_end=date(2027, 1, 1),
    )


def extracted(unit_id: str, rent: str | None = "1000.00", market: str = "1100.00"):
    u = unit(unit_id, rent, market=market, beds=1, baths="1")
    u.sq_ft = 700
    u.lease_start, u.lease_end = date(2026, 1, 1), date(2027, 1, 1)
    return u


def truth_line(label: str, bucket: str, total: str) -> TruthT12Line:
    return TruthT12Line(
        label=label,
        true_bucket=bucket,  # type: ignore[arg-type]
        monthly=[D("0.00")] * 12,
        stated_total=D(total),
        is_nonrecurring=False,
    )


def truth(units, lines=(), expected=(), traps=()):
    return DealTruth(
        deal_id="deal_x",
        seed=7,
        split="dev",
        rr_layout="rr_classic",
        t12_layout="t12_grid",
        rr_format="pdf_text",
        t12_format="pdf_text",
        as_of_date=date(2026, 9, 30),
        units=list(units),
        rr_stated_totals={},
        t12_lines=list(lines),
        traps=list(traps),
        expected=list(expected),
    )


def package(units, findings=(), lines=None, cost="0", wall=1.5) -> DealPackage:
    return DealPackage(
        deal_id="deal_x",
        run_id="r",
        model_id="m",
        rent_roll=roll(list(units)),
        t12=None if lines is None else t12(list(lines)),
        proforma=None,
        findings=list(findings),
        gate_log=[],
        final_state="READY",
        cost_usd=D(cost),
        wall_seconds=wall,
        created_at=NOW,
    )


def finding(check_id="RR_ZERO_RENT_OCC", severity="warn", units=(), labels=()) -> Finding:
    return Finding(
        check_id=check_id,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        message="m",
        unit_ids=list(units),
        line_labels=list(labels),
        citations=[cite()],
    )


def expected(check_id="RR_ZERO_RENT_OCC", trap="TRAP_X", units=(), labels=()) -> ExpectedFinding:
    return ExpectedFinding(
        check_id=check_id,  # type: ignore[arg-type]
        trap_id=trap,
        unit_ids=list(units),
        line_labels=list(labels),
    )


# ---------- units and numeric fields ----------


def test_perfect_extraction_scores_every_numeric_field() -> None:
    score = score_deal(
        package([extracted("1"), extracted("2")]), truth([truth_unit("1"), truth_unit("2")])
    )
    assert (score.numeric_fields_total, score.numeric_fields_correct) == (14, 14)
    assert (score.units_in_doc, score.units_found, score.phantom_units) == (2, 2, 0)
    assert score.provenance_mix == {"text_layer": 14}


def test_a_wrong_money_field_costs_exactly_one_field() -> None:
    score = score_deal(package([extracted("1", rent="1001.00")]), truth([truth_unit("1")]))
    assert (score.numeric_fields_total, score.numeric_fields_correct) == (7, 6)


def test_a_blank_truth_value_is_not_scored() -> None:
    score = score_deal(package([extracted("1", rent=None)]), truth([truth_unit("1", rent=None)]))
    assert score.numeric_fields_total == 6 and score.numeric_fields_correct == 6


def test_missing_and_phantom_units() -> None:
    score = score_deal(
        package([extracted("1"), extracted("ghost")]), truth([truth_unit("1"), truth_unit("2")])
    )
    assert (score.units_in_doc, score.units_found, score.phantom_units) == (2, 1, 1)
    assert (score.numeric_fields_total, score.numeric_fields_correct) == (14, 7)


def test_units_are_matched_by_normalized_id() -> None:
    score = score_deal(package([extracted(" a1 ")]), truth([truth_unit("A1")]))
    assert score.units_found == 1 and score.phantom_units == 0


def test_a_duplicated_printed_row_counts_once_as_truth_and_once_as_extracted() -> None:
    score = score_deal(
        package([extracted("1"), extracted("1")]), truth([truth_unit("1"), truth_unit("1")])
    )
    assert (score.units_in_doc, score.units_found, score.phantom_units) == (1, 1, 0)
    assert score.numeric_fields_total == 7


# ---------- chart of accounts, dollar weighted ----------


def test_coa_accuracy_is_weighted_by_absolute_dollars() -> None:
    truth_lines = [
        truth_line("Rent", "GROSS_POTENTIAL_RENT", "9000.00"),
        truth_line("Vacancy", "VACANCY_LOSS", "-1000.00"),
    ]
    got = [
        line("Rent", "GROSS_POTENTIAL_RENT", "750.00"),
        line("Vacancy", "OTHER_INCOME", "-83.33"),  # wrong bucket
    ]
    score = score_deal(package([], lines=got), truth([], truth_lines))
    assert score.coa_dollars_total == D("10000.00")
    assert score.coa_dollars_correct == D("9000.00")


def test_an_unmatched_label_is_all_wrong_and_no_t12_means_nothing_correct() -> None:
    truth_lines = [truth_line("Rent", "GROSS_POTENTIAL_RENT", "500.00")]
    renamed = score_deal(
        package([], lines=[line("Potential Rent", "GROSS_POTENTIAL_RENT")]), truth([], truth_lines)
    )
    missing = score_deal(package([]), truth([], truth_lines))
    assert renamed.coa_dollars_correct == 0 == missing.coa_dollars_correct
    assert renamed.coa_dollars_total == D("500.00") == missing.coa_dollars_total


# ---------- the matching rule ----------


def test_check_id_alone_matches_when_nothing_specific_is_expected() -> None:
    assert finding_matches(expected(), finding())
    assert not finding_matches(expected(), finding(check_id="RR_DUP_UNIT"))


def test_unit_ids_must_overlap_when_expected_names_units() -> None:
    want = expected(units=["101", "102"])
    assert finding_matches(want, finding(units=["102", "999"]))
    assert not finding_matches(want, finding(units=["999"]))
    assert not finding_matches(want, finding())


def test_line_labels_must_overlap_when_expected_names_lines() -> None:
    want = expected("T12_SPIKE", labels=["R&M"])
    assert finding_matches(want, finding("T12_SPIKE", labels=["R&M", "Gas"]))
    assert not finding_matches(want, finding("T12_SPIKE", labels=["Gas"]))


def test_unit_ids_are_compared_after_normalizing() -> None:
    assert finding_matches(expected(units=["a1"]), finding(units=[" A1"]))


def test_caught_counts_any_severity_including_info() -> None:
    exp = expected(units=["1"])
    for severity in ("info", "warn", "block"):
        score = score_deal(
            package([], [finding(severity=severity, units=["1"])]), truth([], expected=[exp])
        )
        assert (score.planted, score.caught) == (1, 1), severity


def test_an_unmatched_expected_finding_is_a_miss_with_a_reason() -> None:
    none_raised = score_deal(package([]), truth([], expected=[expected(trap="TRAP_A")]))
    wrong_unit = score_deal(
        package([], [finding(units=["9"])]),
        truth([], expected=[expected(trap="TRAP_B", units=["1"])]),
    )
    assert none_raised.caught == 0 and none_raised.misses == [
        "TRAP_A: RR_ZERO_RENT_OCC raised no finding"
    ]
    assert wrong_unit.misses == ["TRAP_B: RR_ZERO_RENT_OCC raised findings that do not name 1"]


# ---------- false positives ----------


def test_info_findings_are_never_false_positives() -> None:
    score = score_deal(package([], [finding(severity="info")]), truth([]))
    assert score.false_positives == 0 and score.clean_control_findings == 0


def test_unexpected_warn_and_block_findings_are_false_positives() -> None:
    findings = [finding(severity="warn"), finding("RR_DUP_UNIT", severity="block")]
    score = score_deal(package([], findings), truth([], expected=[expected("T12_SPIKE")]))
    assert score.false_positives == 2
    assert score.clean_control_findings == 0, "only deals with no traps are clean controls"


def test_a_finding_that_matches_an_expected_one_is_not_a_false_positive() -> None:
    score = score_deal(
        package([], [finding(units=["1"])]), truth([], expected=[expected(units=["1"])])
    )
    assert score.false_positives == 0


def test_clean_control_findings_count_warn_and_block_on_deals_without_traps() -> None:
    findings = [finding(severity="warn"), finding(severity="block"), finding(severity="info")]
    score = score_deal(package([], findings), truth([]))
    assert score.clean_control_findings == 2 == score.false_positives


# ---------- passthrough and failure ----------


def test_cost_wall_time_and_ids_pass_through() -> None:
    score = score_deal(package([], cost="0.0123", wall=2.5), truth([]))
    assert (score.deal_id, score.split) == ("deal_x", "dev")
    assert score.cost_usd == D("0.0123") and score.wall_seconds == 2.5


def test_failed_score_marks_everything_missed() -> None:
    t = truth(
        [truth_unit("1")],
        [truth_line("Rent", "GROSS_POTENTIAL_RENT", "100.00")],
        expected=[expected(trap="TRAP_A")],
    )
    score = failed_score(t, "boom")
    assert (score.numeric_fields_total, score.numeric_fields_correct) == (7, 0)
    assert score.units_found == 0 and score.caught == 0 and score.planted == 1
    assert score.coa_dollars_total == D("100.00") and score.coa_dollars_correct == 0
    assert score.misses == ["TRAP_A: deal failed to process (boom)"]


def test_failed_score_on_a_clean_deal_still_reports_the_failure() -> None:
    score = failed_score(truth([truth_unit("1")]), "boom")
    assert score.misses == ["deal_x: deal failed to process (boom)"]
