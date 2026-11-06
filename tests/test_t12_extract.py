from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cre_screen.config import AppConfig, load_config
from cre_screen.errors import T12ExtractError
from cre_screen.extract.t12 import build_t12
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table, is_month_label
from cre_screen.ingest.raw import RawCell, RawRow, RawTable
from cre_screen.schemas.deal import DealTruth
from cre_screen.schemas.mapping import COAMapping, COAMappingBatch
from cre_screen.schemas.t12 import T12Statement
from tests.fakes import FakeLLMClient

D = Decimal
DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
DEALS = [f"deal_{n:03d}" for n in range(1, 6)]
CFG = load_config()
MONTHS = [
    f"{m}-26"
    for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
]
HEADERS = ["Description", *MONTHS, "Total"]


# ---------- real synthetic deals ----------


@pytest.fixture(scope="module")
def cfg() -> AppConfig:
    return CFG


@pytest.fixture(scope="module")
def extracted(cfg: AppConfig) -> dict[str, tuple[T12Statement, DealTruth, FakeLLMClient]]:
    out = {}
    for name in DEALS:
        truth = DealTruth.model_validate_json((DATA / name / "truth.json").read_bytes())
        # Only labels outside the synonym table reach the LLM: the planted one-time income line.
        canned = {
            ("coa_map", frozenset([ln.label])): COAMappingBatch(
                mappings=[
                    COAMapping(
                        line_label=ln.label,
                        bucket=ln.true_bucket,
                        is_nonrecurring=ln.is_nonrecurring,
                        rationale="canned",
                    )
                ]
            )
            for ln in truth.t12_lines
            if ln.is_nonrecurring
        }
        fake = FakeLLMClient(canned)
        table = extract_pdf_t12_table(DATA / name / "t12.pdf", cfg.coa)
        out[name] = (build_t12(table, cfg, llm=fake), truth, fake)
    return out


@pytest.mark.parametrize("name", DEALS)
def test_lines_buckets_months_and_stated_totals_are_exact(extracted, name: str) -> None:
    t12, truth, _ = extracted[name]
    assert [ln.label for ln in t12.lines] == [ln.label for ln in truth.t12_lines]
    for got, want in zip(t12.lines, truth.t12_lines, strict=True):
        assert got.bucket == want.true_bucket, got.label
        assert got.monthly == want.monthly, got.label
        assert got.stated_total == want.stated_total, got.label
        assert got.is_nonrecurring == want.is_nonrecurring, got.label


@pytest.mark.parametrize("name", DEALS)
def test_period_months_and_property(extracted, name: str) -> None:
    t12, truth, _ = extracted[name]
    assert t12.period_end == truth.as_of_date
    assert len(t12.month_labels) == 12 and all(is_month_label(m) for m in t12.month_labels)
    assert t12.property_name
    assert t12.source_format == "pdf_text" and t12.extraction_warnings == []


@pytest.mark.parametrize("name", DEALS)
def test_every_line_has_a_row_citation_and_13_cell_citations(extracted, name: str) -> None:
    t12, _, _ = extracted[name]
    for ln in t12.lines:
        assert ln.citation.page == 1 and ln.citation.bbox is not None
        assert ln.citation.raw_text == ln.label
        assert set(ln.cell_citations) == {f"m{i:02d}" for i in range(1, 13)} | {"total"}
        assert all(c.provenance == "text_layer" for c in ln.cell_citations.values())


@pytest.mark.parametrize("name", DEALS)
def test_synonym_lines_never_call_the_llm(extracted, name: str) -> None:
    t12, _, fake = extracted[name]
    llm_lines = [ln for ln in t12.lines if ln.mapping_source == "llm"]
    assert len(fake.calls_for("coa_map")) == (1 if llm_lines else 0)
    assert all(
        ln.mapping_confidence == 1.0 for ln in t12.lines if ln.mapping_source == "synonym_table"
    )


def test_the_planted_one_time_line_is_nonrecurring_and_excluded_from_uw(extracted) -> None:
    t12, _, _ = extracted["deal_005"]
    (one_time,) = [ln for ln in t12.lines if ln.is_nonrecurring]
    assert one_time.label in ("Insurance Proceeds", "Lease Buyout Fee")
    assert one_time.bucket == "OTHER_INCOME" and one_time.mapping_source == "llm"
    assert one_time.mapping_confidence == 0.7 and one_time.include_in_uw is False


def test_deductions_are_stored_negative(extracted) -> None:
    t12, _, _ = extracted["deal_001"]
    assert t12.bucket_total("VACANCY_LOSS") < 0 and t12.bucket_total("LOSS_TO_LEASE") < 0
    assert t12.bucket_total("GROSS_POTENTIAL_RENT") > 0


def test_pdf_table_kinds_for_a_synthetic_t12() -> None:
    table = extract_pdf_t12_table(DATA / "deal_001" / "t12.pdf", CFG.coa)
    kinds = [r.kind for r in table.rows]
    assert kinds.count("line") == 16 and kinds.count("header") == 1
    assert table.headers[0] and table.headers[-1] == "Total" and len(table.headers) == 14


# ---------- hand-built tables ----------


def cell(text: str) -> RawCell:
    return RawCell(text, (10.0, 100.0, 50.0, 110.0), None)


def row(index: int, label: str, values: list[str | None], total: str | None, kind="line") -> RawRow:
    cells = {"Description": cell(label)}
    for header, value in zip(MONTHS, values, strict=True):
        if value is not None:
            cells[header] = cell(value)
    if total is not None:
        cells["Total"] = cell(total)
    return RawRow(page=1, sheet=None, row_index=index, kind=kind, cells=cells)


def table(*rows: RawRow, headers: list[str] | None = None) -> RawTable:
    return RawTable(
        source_file="t12.pdf",
        source_format="pdf_text",
        headers=headers or HEADERS,
        rows=list(rows),
        page_count=1,
        header_texts_by_page={1: HEADERS},
        preamble_text="Birch Court\nTrailing 12",
    )


def flat(value: str) -> list[str | None]:
    return [value] * 12


def build(*rows: RawRow, llm: FakeLLMClient | None = None, **kw) -> T12Statement:
    return build_t12(table(*rows, **kw), CFG, llm=llm or FakeLLMClient())


def test_stated_subtotals_become_stated_subtotal_not_lines() -> None:
    t12 = build(
        row(0, "Potential Rent", flat("1,000.00"), "12,000.00"),
        row(1, "Marketing", flat("100.00"), "1,200.00"),
        row(2, "Total Income", flat("1,000.00"), "12,000.00", kind="subtotal"),
        row(3, "Total Operating Expenses", flat("100.00"), "1,200.00", kind="subtotal"),
        row(4, "Net Operating Income", flat("900.00"), "10,800.00", kind="subtotal"),
        row(5, "Total Misc", flat("1.00"), "12.00", kind="subtotal"),
    )
    assert [ln.label for ln in t12.lines] == ["Potential Rent", "Marketing"]
    assert [(s.label, s.kind) for s in t12.stated_subtotals] == [
        ("Total Income", "total_income"),
        ("Total Operating Expenses", "total_expenses"),
        ("Net Operating Income", "noi"),
        ("Total Misc", "other"),
    ]
    noi = t12.stated_subtotals[2]
    assert noi.total == D("10800.00") and noi.monthly[0] == D("900.00")
    assert noi.citation.page == 1


def test_missing_subtotal_months_stay_none() -> None:
    values = flat("5.00")
    values[3] = None
    t12 = build(row(0, "Total Income", values, None, kind="subtotal"))
    assert t12.stated_subtotals[0].monthly[3] is None and t12.stated_subtotals[0].total is None


def test_deduction_printed_positive_is_flipped_and_flagged() -> None:
    t12 = build(row(0, "Vacancy", flat("500.00"), "6,000.00"))
    (line,) = t12.lines
    assert line.monthly == [D("-500.00")] * 12 and line.stated_total == D("-6000.00")
    assert "sign_normalized" in line.audit_flags


def test_deduction_already_negative_is_untouched() -> None:
    t12 = build(row(0, "Vacancy", flat("(500.00)"), "(6,000.00)"))
    (line,) = t12.lines
    assert line.monthly == [D("-500.00")] * 12 and "sign_normalized" not in line.audit_flags


def test_expense_with_a_credit_month_is_not_flipped() -> None:
    values = flat("100.00")
    values[5] = "(40.00)"
    (line,) = build(row(0, "Marketing", values, None)).lines
    assert line.monthly[5] == D("-40.00") and "sign_normalized" not in line.audit_flags


def test_blank_month_becomes_zero_with_a_flag() -> None:
    values = flat("10.00")
    values[2] = None
    (line,) = build(row(0, "Marketing", values, "110.00")).lines
    assert line.monthly[2] == D("0.00") and "blank_months:m03" in line.audit_flags
    assert "m03" not in line.cell_citations


def test_accounting_dash_is_a_zero_month() -> None:
    values = flat("10.00")
    values[0] = "-"
    (line,) = build(row(0, "Marketing", values, None)).lines
    assert line.monthly[0] == D("0.00") and not line.audit_flags


def test_unparseable_cell_is_zero_with_flag_and_warning() -> None:
    values = flat("10.00")
    values[1] = "see note"
    t12 = build(row(0, "Marketing", values, None))
    assert "unparseable_Marketing m02" in t12.lines[0].audit_flags
    assert any("Marketing m02" in w for w in t12.extraction_warnings)


def test_duplicate_labels_get_a_numeric_suffix() -> None:
    t12 = build(
        row(0, "Marketing", flat("10.00"), None),
        row(1, "Marketing", flat("20.00"), None),
        row(2, "Marketing", flat("30.00"), None),
    )
    assert [ln.label for ln in t12.lines] == ["Marketing", "Marketing (2)", "Marketing (3)"]
    assert "duplicate_label" in t12.lines[1].audit_flags
    assert all(ln.bucket == "MARKETING" for ln in t12.lines)


def test_missing_stated_total_is_none() -> None:
    (line,) = build(row(0, "Marketing", flat("10.00"), None)).lines
    assert line.stated_total is None and "total" not in line.cell_citations


def test_fuzzy_line_records_its_score_as_confidence() -> None:
    (line,) = build(row(0, "Real Estate Tax", flat("10.00"), None)).lines
    assert (line.bucket, line.mapping_source) == ("REAL_ESTATE_TAXES", "fuzzy")
    assert 0.92 <= line.mapping_confidence < 1.0


def test_unmapped_label_after_llm_failure_is_kept_as_unmapped_line() -> None:
    fake = FakeLLMClient({("coa_map", frozenset(["Alpha Item"])): COAMappingBatch(mappings=[])})
    (line,) = build(row(0, "Alpha Item", flat("10.00"), None), llm=fake).lines
    assert line.bucket == "UNMAPPED" and line.include_in_uw is False
    assert len(fake.calls_for("coa_map")) == 2


def test_period_end_is_the_end_of_the_last_month() -> None:
    assert build(row(0, "Marketing", flat("1.00"), None)).period_end.isoformat() == "2026-12-31"


def test_month_labels_in_other_formats_are_accepted() -> None:
    labels = [f"{m} 2025" for m in ("Oct", "Nov", "Dec")] + [
        f"{m} 2026" for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep")
    ]
    headers = ["Account", *labels, "Total"]
    cells = {"Account": cell("Marketing")} | {h: cell("1.00") for h in labels}
    t = RawTable(
        "t12.pdf", "pdf_text", headers, [RawRow(1, None, 0, "line", cells)], 1, {1: headers}, ""
    )
    t12 = build_t12(t, CFG, llm=FakeLLMClient())
    assert t12.period_end.isoformat() == "2026-09-30" and t12.month_labels == labels


def test_wrong_number_of_month_columns_is_an_error() -> None:
    with pytest.raises(T12ExtractError, match="expected 12 month columns, found 11"):
        build(row(0, "Marketing", flat("1.00"), None), headers=[*HEADERS[:-2], "Total"])


def test_table_without_headers_is_an_error() -> None:
    with pytest.raises(T12ExtractError, match="no header row"):
        build_t12(RawTable("t12.pdf", "pdf_text", [], [], 1, {}, ""), CFG, llm=FakeLLMClient())
