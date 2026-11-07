from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.config import ThresholdsConfig, load_config
from cre_screen.extract.t12 import build_t12
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table
from cre_screen.reconcile.checks import REGISTRY
from cre_screen.reconcile.checks.t12_footing import check, footing_findings
from cre_screen.reconcile.context import CheckContext
from cre_screen.schemas.citation import Citation
from cre_screen.schemas.deal import DealTruth
from cre_screen.schemas.t12 import StatedSubtotal, T12Line, T12Statement
from tests.fakes import FakeLLMClient

D = Decimal
CFG = load_config()
THRESHOLDS = CFG.thresholds
DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
LABELS = [f"M{i}" for i in range(1, 13)]


def cite(row: int = 0) -> Citation:
    return Citation(
        source_file="t12.pdf", page=1, row_index=row, raw_text="x", provenance="text_layer"
    )


def line(label: str, bucket: str, monthly: list[str], stated: str | None, row: int = 0) -> T12Line:
    return T12Line(
        label=label,
        bucket=bucket,  # type: ignore[arg-type]
        monthly=[D(m) for m in monthly],
        stated_total=None if stated is None else D(stated),
        mapping_source="synonym_table",
        mapping_confidence=1.0,
        citation=cite(row),
    )


def sub(
    label: str, kind: str, monthly: list[str | None], total: str | None, row: int = 50
) -> StatedSubtotal:
    return StatedSubtotal(
        label=label,
        kind=kind,  # type: ignore[arg-type]
        monthly=[None if m is None else D(m) for m in monthly],
        total=None if total is None else D(total),
        citation=cite(row),
    )


def statement(lines: list[T12Line], subtotals: list[StatedSubtotal] | None = None) -> T12Statement:
    return T12Statement(
        period_end=date(2026, 12, 31),
        month_labels=LABELS,
        source_file="t12.pdf",
        source_format="pdf_text",
        lines=lines,
        stated_subtotals=subtotals or [],
    )


def run(t12: T12Statement | None, thresholds: ThresholdsConfig = THRESHOLDS):
    from cre_screen.schemas.rent_roll import ColumnMap, HeaderMap, RentRoll

    rr = RentRoll(
        source_file="rent_roll.pdf",
        source_format="pdf_text",
        header_map=HeaderMap(
            columns=[
                ColumnMap(header_text="Unit", maps_to="unit_id"),
                ColumnMap(header_text="Rent", maps_to="in_place_rent"),
                ColumnMap(header_text="Status", maps_to="status"),
            ],
            source="synonym_table",
        ),
        units=[],
    )
    return check(
        CheckContext(
            rent_roll=rr, t12=t12, proforma=None, thresholds=thresholds, as_of=date(2026, 12, 31)
        )
    )


RENT = line("Potential Rent", "GROSS_POTENTIAL_RENT", ["1000.00"] * 12, "12000.00")
TAXES = line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, "1200.00", row=1)
MKTG = line("Marketing", "MARKETING", ["50.00"] * 12, "600.00", row=2)


# ---------- line totals ----------


def test_lines_that_foot_pass() -> None:
    assert run(statement([RENT, TAXES])) == []


def test_line_total_off_by_exactly_one_dollar_passes() -> None:
    off = line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, "1201.00")
    assert run(statement([off])) == []
    assert run(statement([line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, "1199.00")])) == []


def test_line_total_off_by_one_cent_more_fails() -> None:
    off = line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, "1201.01", row=7)
    (f,) = run(statement([off]))
    assert f.check_id == "T12_FOOTING" and f.severity == "block"
    assert f.delta == D("-1.01")
    assert f.line_labels == ["Taxes"] and f.citations == [cite(7)]
    assert f.details == {"kind": "line", "computed": "1200.00", "stated": "1201.01"}


def test_changed_month_with_unchanged_stated_total_is_caught() -> None:
    months = ["100.00"] * 12
    months[4] = "130.00"
    (f,) = run(statement([line("Taxes", "REAL_ESTATE_TAXES", months, "1200.00")]))
    assert f.delta == D("30.00")


def test_one_finding_per_failing_line() -> None:
    bad1 = line("A", "MARKETING", ["10.00"] * 12, "999.00")
    bad2 = line("B", "MARKETING", ["10.00"] * 12, "5.00", row=1)
    findings = run(statement([bad1, TAXES, bad2]))
    assert [f.line_labels for f in findings] == [["A"], ["B"]]


def test_line_without_a_stated_total_is_skipped() -> None:
    assert run(statement([line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, None)])) == []


def test_line_tolerance_comes_from_the_thresholds_file() -> None:
    loose = THRESHOLDS.model_copy(
        update={
            "t12_footing": THRESHOLDS.t12_footing.model_copy(
                update={"line_tolerance_usd": D("10.00")}
            )
        }
    )
    off = line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, "1210.00")
    assert run(statement([off]), loose) == []
    assert (
        len(run(statement([line("Taxes", "REAL_ESTATE_TAXES", ["100.00"] * 12, "1210.01")]), loose))
        == 1
    )


# ---------- missing input ----------


def test_no_t12_gives_no_findings() -> None:
    assert run(None) == []


def test_empty_statement_gives_no_findings() -> None:
    assert run(statement([])) == []


def test_statement_with_no_subtotals_is_checked_on_lines_only() -> None:
    assert run(statement([RENT, TAXES, MKTG])) == []


# ---------- stated subtotals ----------


INCOME_OK = sub("Total Income", "total_income", ["1000.00"] * 12, "12000.00")
EXPENSES_OK = sub("Total Expenses", "total_expenses", ["150.00"] * 12, "1800.00", row=51)
NOI_OK = sub("NOI", "noi", ["850.00"] * 12, "10200.00", row=52)
BASE_LINES = [RENT, TAXES, MKTG]


def test_subtotals_that_foot_pass() -> None:
    assert run(statement(BASE_LINES, [INCOME_OK, EXPENSES_OK, NOI_OK])) == []


def test_subtotal_total_off_by_exactly_five_dollars_passes_and_a_cent_more_fails() -> None:
    at_edge = sub("Total Income", "total_income", ["1000.00"] * 12, "12005.00")
    assert run(statement(BASE_LINES, [at_edge])) == []
    over = sub("Total Income", "total_income", ["1000.00"] * 12, "12005.01")
    (f,) = run(statement(BASE_LINES, [over]))
    assert f.details["kind"] == "subtotal" and f.details["failed"] == "total"
    assert f.delta == D("5.01") and f.line_labels == ["Total Income"]
    assert f.citations == [cite(50)]


def test_subtotal_month_edge() -> None:
    months: list[str | None] = ["1000.00"] * 12
    months[3] = "1005.00"
    assert run(statement(BASE_LINES, [sub("Total Income", "total_income", months, None)])) == []
    months[3] = "1005.01"
    (f,) = run(statement(BASE_LINES, [sub("Total Income", "total_income", months, None)]))
    assert f.details["failed"] == "M4"


def test_one_finding_per_subtotal_lists_every_failing_month() -> None:
    months: list[str | None] = ["1000.00"] * 12
    months[0], months[11] = "900.00", "1100.00"
    (f,) = run(statement(BASE_LINES, [sub("Total Income", "total_income", months, "12000.00")]))
    assert f.details["failed"] == "M1,M12"
    assert abs(f.delta) == D("100.00")


def test_blank_subtotal_months_are_skipped() -> None:
    months: list[str | None] = [None] * 12
    months[0] = "1000.00"
    assert run(statement(BASE_LINES, [sub("Total Income", "total_income", months, None)])) == []


def test_total_expenses_sums_only_opex_buckets() -> None:
    capex = line("Reserves", "CAPEX_RESERVES", ["10.00"] * 12, "120.00", row=3)
    ok = sub("Total Expenses", "total_expenses", ["150.00"] * 12, "1800.00")
    assert run(statement([RENT, TAXES, MKTG, capex], [ok])) == []


def test_noi_is_checked_against_stated_income_minus_expenses_when_both_exist() -> None:
    wrong_income = sub("Total Income", "total_income", ["1000.00"] * 12, "12000.00")
    # NOI matches stated income minus stated expenses, so only the expense subtotal is wrong
    bad_expenses = sub("Total Expenses", "total_expenses", ["200.00"] * 12, "2400.00", row=51)
    noi = sub("NOI", "noi", ["800.00"] * 12, "9600.00", row=52)
    findings = run(statement(BASE_LINES, [wrong_income, bad_expenses, noi]))
    assert [f.line_labels for f in findings] == [["Total Expenses"]]


def test_noi_alone_is_checked_against_the_computed_figure() -> None:
    bad = sub("NOI", "noi", ["850.00"] * 12, "10300.00")
    (f,) = run(statement(BASE_LINES, [bad]))
    assert f.details["subtotal_kind"] == "noi" and f.delta == D("100.00")


def test_other_subtotals_are_not_checked() -> None:
    assert run(statement(BASE_LINES, [sub("Total Misc", "other", ["5.00"] * 12, "60.00")])) == []


def test_subtotal_tolerance_comes_from_the_thresholds_file() -> None:
    tight = THRESHOLDS.model_copy(
        update={
            "t12_footing": THRESHOLDS.t12_footing.model_copy(
                update={"subtotal_tolerance_usd": D("1.00")}
            )
        }
    )
    off = sub("Total Income", "total_income", ["1000.00"] * 12, "12002.00")
    assert run(statement(BASE_LINES, [off])) == []
    assert len(run(statement(BASE_LINES, [off]), tight)) == 1


# ---------- registry and synthetic deals ----------


def test_check_is_registered_after_rr_footing() -> None:
    assert "T12_FOOTING" in REGISTRY


def extracted(name: str) -> tuple[T12Statement, DealTruth]:
    table = extract_pdf_t12_table(DATA / name / "t12.pdf", CFG.coa)
    truth = DealTruth.model_validate_json((DATA / name / "truth.json").read_bytes())
    return build_t12(table, CFG, llm=_one_time_llm(truth)), truth


def _one_time_llm(truth: DealTruth) -> FakeLLMClient:
    from cre_screen.schemas.mapping import COAMapping, COAMappingBatch

    return FakeLLMClient(
        {
            ("coa_map", frozenset([ln.label])): COAMappingBatch(
                mappings=[
                    COAMapping(
                        line_label=ln.label,
                        bucket=ln.true_bucket,
                        is_nonrecurring=True,
                        rationale="canned",
                    )
                ]
            )
            for ln in truth.t12_lines
            if ln.is_nonrecurring
        }
    )


@pytest.mark.parametrize("name", ["deal_001", "deal_002", "deal_003", "deal_005"])
def test_deals_without_the_trap_have_no_footing_findings(name: str) -> None:
    t12, _ = extracted(name)
    assert footing_findings(t12, THRESHOLDS) == []


def test_planted_trap_is_caught_on_exactly_the_planted_line() -> None:
    t12, truth = extracted("deal_004")
    (expected,) = [e for e in truth.expected if e.check_id == "T12_FOOTING"]
    findings = footing_findings(t12, THRESHOLDS)
    assert [f.line_labels for f in findings] == [expected.line_labels]
    assert findings[0].severity == "block" and abs(findings[0].delta) > D("1.00")
