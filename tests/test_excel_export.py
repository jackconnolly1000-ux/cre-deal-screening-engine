from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from openpyxl.worksheet.worksheet import Worksheet

from cre_screen.config import load_config
from cre_screen.errors import ExportError
from cre_screen.export import layout as L
from cre_screen.export.excel_writer import build_workbook, write_workbook
from cre_screen.export.uw_math import Assumptions, compute_uw, debt_service, pmt
from cre_screen.mapping.llm_protocol import NullLLMClient
from cre_screen.pipeline import run_deal
from cre_screen.schemas.deal import DealPackage
from cre_screen.schemas.mapping import COAMapping, COAMappingBatch
from tests.builders import cite, concession, line, proforma, roll, t12, unit
from tests.fakes import FakeLLMClient

D = Decimal
DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
CFG = load_config()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SECTION_ROWS = set(L.SECTION_LABEL_ROWS)
RATIO_ROWS = {L.ROW_CAP_RATE, L.ROW_DSCR, L.ROW_DEBT_YIELD, L.ROW_PHYS_OCC, L.ROW_ECON_OCC}
SPEC_NAMES = [
    "AskingPrice", "EntryCapRate", "PurchasePrice", "LTV", "LoanAmount", "InterestRate",
    "AmortYears", "IOPeriodMonths", "ExpenseGrowth", "MgmtFeePct", "ReservesPerUnit",
    "UnitCount", "AssessmentRatio", "TaxRate", "ReassessedTaxBump", "ReassessedTax",
]  # fmt: skip


# ---------- fixtures ----------


def make_package(name: str, *, llm=None) -> DealPackage:
    return run_deal(DATA / name, llm=llm or NullLLMClient(), cfg=CFG, out_dir=Path("out"))


@pytest.fixture(scope="module")
def packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, DealPackage]:
    out = tmp_path_factory.mktemp("pkg")
    return {
        n: run_deal(DATA / n, llm=NullLLMClient(), cfg=CFG, out_dir=out)
        for n in ("deal_001", "deal_003", "deal_007")
    }


@pytest.fixture(scope="module")
def workbooks(packages, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("xlsx")
    return {n: write_workbook(p, out / f"{n}.xlsx", synthetic=True) for n, p in packages.items()}


@lru_cache(maxsize=16)
def recalculated(path: str) -> dict[tuple[str, str], Any]:
    """{(SHEET, "E24"): value} from a headless recalculation with the `formulas` package."""
    import formulas

    solution = formulas.ExcelModel().loads(path).finish().calculate()
    values: dict[tuple[str, str], Any] = {}
    for key, cell in solution.items():
        match = re.match(r"^'\[[^\]]+\]([^']+)'!([A-Z]+[0-9]+)$", str(key).upper())
        if match is None:
            continue
        raw = cell.value[0][0] if hasattr(cell, "value") else cell
        values[(match.group(1), match.group(2))] = raw
    return values


def pf_value(values: dict, cell: str) -> Any:
    return values[(L.PROFORMA.upper(), cell)]


def formula_cells(ws: Worksheet):
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("="):
                yield c


# ---------- the six tests the SPEC names ----------


def test_no_hardcoded_outputs(workbooks) -> None:
    for path in workbooks.values():
        wb = openpyxl.load_workbook(path)
        pf = wb[L.PROFORMA]
        for row in range(L.PF_FIRST_ROW, L.PF_LAST_ROW + 1):
            if row in SECTION_ROWS:
                continue
            for column in L.PF_COLUMNS:
                value = pf[f"{column}{row}"].value
                if row in L.SECTION_LABEL_ROWS or (value is None and row in (12, 27, 34)):
                    continue
                assert isinstance(value, str) and value.startswith("="), f"{column}{row}: {value!r}"
        for ref in ("B4", "B6", "B13", "B17"):
            assert str(wb[L.ASSUMPTIONS][ref].value).startswith("="), ref
        rr, tm = wb[L.RENT_ROLL], wb[L.T12]
        for r in range(L.RR_FIRST, L.RR_FIRST + sum(1 for c in rr["A"][1:] if c.value)):
            assert str(rr[f"L{r}"].value).startswith("="), f"Rent_Roll!L{r}"
        for r in range(L.T12_FIRST, L.T12_FIRST + sum(1 for c in tm["A"][1:] if c.value)):
            assert str(tm[f"Q{r}"].value).startswith("="), f"T12_Mapped!Q{r}"
            assert str(tm[f"S{r}"].value).startswith("="), f"T12_Mapped!S{r}"


def test_no_whole_column_refs(workbooks) -> None:
    whole_a = re.compile(r"![A-Z]+:\$?[A-Z]+(?![0-9$])")
    whole_b = re.compile(r"\$[A-Z]+:\$[A-Z]+(?![0-9])")
    checked = 0
    for path in workbooks.values():
        wb = openpyxl.load_workbook(path)
        for ws in wb.worksheets:
            for c in formula_cells(ws):
                checked += 1
                assert not whole_a.search(c.value), f"{ws.title}!{c.coordinate}: {c.value}"
                assert not whole_b.search(c.value), f"{ws.title}!{c.coordinate}: {c.value}"
        for name in wb.defined_names.values():
            assert not whole_b.search(name.attr_text)
    assert checked > 500


def test_named_ranges(workbooks) -> None:
    wb = openpyxl.load_workbook(workbooks["deal_001"])
    names = {n: d.attr_text for n, d in wb.defined_names.items()}
    assert len(SPEC_NAMES) == 16
    for index, name in enumerate(SPEC_NAMES):
        assert names[name] == f"Assumptions!$B${index + 2}", name
    assert names["BrokerNOI"] == "ProForma_NOI_DSCR!$D$24"
    assert names["T12Taxes"] == "ProForma_NOI_DSCR!$B$13"
    assert set(names) == {*SPEC_NAMES, "BrokerNOI", "T12Taxes"}
    for index, name in enumerate(SPEC_NAMES):
        assert wb[L.ASSUMPTIONS][f"A{index + 2}"].value == name


@pytest.mark.parametrize("name", ["deal_001", "deal_003", "deal_007"])
def test_excel_matches_python(name: str, packages, workbooks) -> None:
    values = recalculated(str(workbooks[name]))
    python = compute_uw(packages[name])

    pf = openpyxl.load_workbook(workbooks[name])[L.PROFORMA]
    formula_addresses = {
        f"{c}{r}"
        for r in range(L.PF_FIRST_ROW, L.PF_LAST_ROW + 1)
        for c in L.PF_COLUMNS
        if isinstance(pf[f"{c}{r}"].value, str)
    }
    assert formula_addresses == set(python), "uw_math must mirror every formula cell exactly"

    for address, expected in python.items():
        got = pf_value(values, address)
        assert not isinstance(got, str), f"{address} is an error value: {got}"
        tolerance = 1e-6 if int(address[1:]) in RATIO_ROWS else 0.01
        assert abs(float(got) - float(expected)) <= tolerance, (
            f"{name} {address}: {got} vs {expected}"
        )


@pytest.mark.parametrize("io_months", [0, 6, 24])
def test_io_blend(io_months: int, packages, workbooks, tmp_path: Path) -> None:
    path = workbooks["deal_003"]
    if io_months:
        wb = openpyxl.load_workbook(path)
        wb[L.ASSUMPTIONS]["B9"] = io_months
        path = tmp_path / f"io_{io_months}.xlsx"
        wb.save(path)
    values = recalculated(str(path))
    python = compute_uw(packages["deal_003"], Assumptions(io_period_months=io_months))
    got = float(pf_value(values, "E31"))
    assert abs(got - float(python["E31"])) <= 0.01
    for column in "BCD":
        assert abs(float(pf_value(values, f"{column}31")) - got) <= 0.01
    assert abs(float(pf_value(values, "E32")) - float(python["E32"])) <= 1e-6


def test_citation_comments(workbooks) -> None:
    for path in workbooks.values():
        wb = openpyxl.load_workbook(path)
        audit, rr = wb[L.AUDIT], wb[L.RENT_ROLL]
        audit_rows = [c for c in audit["B"][1:] if c.value is not None]
        rr_rows = [c for c in rr["G"][1:] if rr[f"A{c.row}"].value]
        assert audit_rows and rr_rows
        for cell in [*audit_rows, *rr_rows]:
            assert cell.comment is not None, f"{cell.parent.title}!{cell.coordinate}"
            assert "Source:" in cell.comment.text and cell.comment.author == "cre-screen"
            assert "Raw:" in cell.comment.text and "Via:" in cell.comment.text


# ---------- no error values anywhere after recalculation ----------


@pytest.mark.parametrize("name", ["deal_001", "deal_003", "deal_007"])
def test_recalculation_shows_no_excel_errors(name: str, workbooks) -> None:
    values = recalculated(str(workbooks[name]))
    errors = {k: v for k, v in values.items() if isinstance(v, str) and v.startswith("#")}
    assert errors == {}, "the owner opens this in Excel: no #REF!, #NAME? or #DIV/0!"


# ---------- workbook contents ----------


@pytest.fixture(scope="module")
def deal3(workbooks) -> openpyxl.Workbook:
    return openpyxl.load_workbook(workbooks["deal_003"])


def test_seven_tabs_in_order(deal3) -> None:
    assert deal3.sheetnames == list(L.SHEET_ORDER) and len(deal3.sheetnames) == 7


def test_readme_cells(packages, deal3) -> None:
    ws, pkg = deal3[L.README], packages["deal_003"]
    assert ws["A1"].value == "cre-screen underwriting export"
    labels = {ws[f"A{r}"].value: ws[f"B{r}"].value for r in range(3, 12)}
    assert labels == {
        "Deal ID": "deal_003",
        "Run ID": pkg.run_id,
        "Generated (UTC)": pkg.created_at.isoformat(),
        "Model": "claude-haiku-5-5",
        "Gate state": "REVIEW",
        "Open block findings": 1,
        "Open warn findings": 2,
        "Rent roll source": "rent_roll.pdf",
        "T-12 source": "t12.pdf",
    }
    assert ws["A13"].value == "Synthetic test data; not a real property."
    assert ws["A14"].value.startswith("Blue = input. Black = formula.")


def test_synthetic_note_is_only_for_synthetic_deals(packages, tmp_path: Path) -> None:
    real = write_workbook(packages["deal_001"], tmp_path / "real.xlsx", synthetic=False)
    assert openpyxl.load_workbook(real)[L.README]["A13"].value is None


def test_assumption_inputs_and_fonts(packages, deal3) -> None:
    ws = deal3[L.ASSUMPTIONS]
    defaults = {3: 0.0625, 5: 0.65, 7: 0.065, 8: 30, 9: 0, 10: 0.03, 11: 0.03, 12: 250, 16: 1.15}
    for row, value in defaults.items():
        assert ws[f"B{row}"].value == value, row
        assert ws[f"B{row}"].font.color.rgb.endswith(L.BLUE), f"B{row} should be blue"
    assert ws["B2"].value == float(packages["deal_003"].proforma.asking_price)
    assert ws["B14"].value is None and ws["B15"].value is None
    for row in (4, 6, 13, 17):
        assert ws[f"B{row}"].font.color.rgb.endswith(L.BLACK), f"B{row} should be black"
    assert ws["B13"].value == "=COUNTA(Rent_Roll!$A$2:$A$2001)"


def test_broker_pro_forma_block_is_in_coa_order_in_rows_21_to_40(packages, deal3) -> None:
    ws, pkg = deal3[L.ASSUMPTIONS], packages["deal_003"]
    rows = [(ws[f"A{r}"].value, ws[f"B{r}"].value) for r in range(21, 41) if ws[f"A{r}"].value]
    assert [b for b, _ in rows] == list(pkg.proforma.lines)
    assert [v for _, v in rows] == [float(v) for v in pkg.proforma.lines.values()]
    assert ws["A20"].value == "Broker pro forma (annual, from proforma.yaml)"
    assert ws["C21"].value == pkg.proforma.source_note
    assert ws["B21"].font.color.rgb.endswith(L.BLUE)


def test_rent_roll_tab(packages, deal3) -> None:
    ws, pkg = deal3[L.RENT_ROLL], packages["deal_003"]
    assert [c.value for c in ws[1]] == [label for _col, label in L.RR_COLUMNS]
    last = 1 + len(pkg.rent_roll.units)
    assert ws.tables["RentRollTable"].ref == f"A1:N{last}"
    first = pkg.rent_roll.units[0]
    assert ws["A2"].value == first.unit_id and ws["F2"].value == first.status
    assert ws["G2"].value == float(first.in_place_rent) and ws["L2"].value == "=G2-K2"
    assert ws["G2"].font.color.rgb.endswith(L.BLUE) and ws["L2"].font.color.rgb.endswith(L.BLACK)
    assert ws["N2"].value == first.citations["unit_id"].short()
    assert ws[f"A{last + 1}"].value is None, "only extracted rows are written"
    flagged = {
        ws[f"A{r}"].value: ws[f"M{r}"].value for r in range(2, last + 1) if ws[f"M{r}"].value
    }
    assert flagged["204"] == "RR_ZERO_RENT_OCC"


def test_vacant_units_have_blank_rent_but_still_carry_a_comment(packages, deal3) -> None:
    ws, pkg = deal3[L.RENT_ROLL], packages["deal_003"]
    row = 2 + next(i for i, u in enumerate(pkg.rent_roll.units) if u.in_place_rent is None)
    assert ws[f"G{row}"].value is None and "Source:" in ws[f"G{row}"].comment.text


def test_t12_tab(packages, deal3) -> None:
    ws, pkg = deal3[L.T12], packages["deal_003"]
    assert [ws.cell(1, c).value for c in range(5, 17)] == pkg.t12.month_labels
    assert [ws[f"{c}1"].value for c in "ABCDQRSTU"] == [
        "Label", "Bucket", "Include_In_UW", "Nonrecurring", "Total", "Stated Total",
        "T3_Annualized", "Mapping Source", "Source",
    ]  # fmt: skip
    first = pkg.t12.lines[0]
    assert (ws["A2"].value, ws["B2"].value, ws["C2"].value, ws["D2"].value) == (
        first.label, first.bucket, 1, 0,
    )  # fmt: skip
    assert ws["E2"].value == float(first.monthly[0]) and ws["P2"].value == float(first.monthly[11])
    assert ws["Q2"].value == "=SUM(E2:P2)" and ws["S2"].value == "=SUM(N2:P2)*4"
    assert ws["R2"].value == float(first.stated_total) and "Source:" in ws["R2"].comment.text
    assert ws["T2"].value == "synonym_table" and ws["U2"].value == first.citation.short()


def test_t12_include_flags_follow_the_schema_for_one_time_income() -> None:
    canned = FakeLLMClient(
        {
            ("coa_map", frozenset([label])): COAMappingBatch(
                mappings=[
                    COAMapping(
                        line_label=label, bucket="OTHER_INCOME", is_nonrecurring=True, rationale="x"
                    )
                ]
            )
            for label in ("Lease Buyout Fee", "Insurance Proceeds")
        }
    )
    pkg = make_package("deal_005", llm=canned)
    ws = build_workbook(pkg)[L.T12]
    rows = {ws[f"A{r}"].value: r for r in range(2, 30) if ws[f"A{r}"].value}
    one_time = next(label for label in rows if label in ("Lease Buyout Fee", "Insurance Proceeds"))
    assert (ws[f"C{rows[one_time]}"].value, ws[f"D{rows[one_time]}"].value) == (0, 1)


def test_findings_tab_is_sorted_block_warn_info_then_check_id(packages, deal3) -> None:
    ws = deal3[L.FINDINGS]
    assert [c.value for c in ws[1]] == list(L.FINDINGS_COLUMNS)
    rows = [
        (ws[f"B{r}"].value, ws[f"A{r}"].value)
        for r in range(2, 2 + len(packages["deal_003"].findings))
    ]
    assert rows == [
        ("block", "RR_FOOTING"),
        ("warn", "RR_ZERO_RENT_OCC"),
        ("warn", "T12_SPIKE"),
        ("info", "RR_ZERO_RENT_OCC"),
    ]
    assert ws["C2"].value == "open" and ws["E2"].value == float(D("-36000.00"))
    assert "rent_roll.pdf" in ws["G2"].value


def test_audit_tab_lists_totals_dates_and_evidence_for_open_findings(packages, deal3) -> None:
    ws = deal3[L.AUDIT]
    assert [c.value for c in ws[1]] == list(L.AUDIT_COLUMNS)
    items = {ws[f"A{r}"].value: ws[f"B{r}"].value for r in range(2, ws.max_row + 1)}
    assert items["Rent roll stated total: in_place_rent"] == 73703.0
    assert items["Rent roll stated total: market_rent"] == 45269.0
    assert items["As-of date"] == datetime(2026, 10, 31)
    assert items["T-12 period end"] == datetime(2026, 10, 31)
    assert any(k.startswith("RR_FOOTING (open)") for k in items)
    row = next(
        r
        for r in range(2, ws.max_row + 1)
        if ws[f"A{r}"].value == "Rent roll stated total: in_place_rent"
    )
    assert ws[f"C{row}"].value == "rent_roll.pdf" and ws[f"D{row}"].value.startswith("p.2 row")
    assert ws[f"F{row}"].value == "text_layer"
    assert ws[f"E{row}"].value.replace("$", "").replace(",", "") in ("73703", "73703.00")


def test_pro_forma_tab_layout(deal3) -> None:
    ws = deal3[L.PROFORMA]
    assert [ws.cell(2, c).value for c in range(1, 7)] == [
        "Line", "T-12 Actual", "T-3 Annualized", "Broker Pro Forma", "Underwritten", "Notes",
    ]  # fmt: skip
    assert [ws[f"A{r}"].value for r in (3, 12, 27, 34)] == [
        "Revenue", "Operating expenses", "Valuation and debt", "Operations",
    ]  # fmt: skip
    assert ws["A4"].value == "GROSS_POTENTIAL_RENT" and ws["A22"].value == "MARKETING"
    assert ws["E13"].value.startswith("=MAX(SUMIFS(T12_Mapped!$Q$2:$Q$201")
    assert ws["E13"].value.endswith(",ReassessedTax)")
    assert ws["E4"].value == (
        "=SUMIFS(T12_Mapped!$S$2:$S$201,T12_Mapped!$B$2:$B$201,"
        '"GROSS_POTENTIAL_RENT",T12_Mapped!$C$2:$C$201,1)'
    )
    assert ws["E9"].value.startswith("=SUMIFS(T12_Mapped!$Q$2:$Q$201")
    assert ws["E25"].value == "=ReservesPerUnit*UnitCount"
    assert ws["B35"].value == "=E35" and ws["B37"].value == "=E37"
    assert ws["C36"].value == "=IFERROR(SUM(C4:C8)/C4,0)"
    assert ws["E31"].value == (
        "=MIN(IOPeriodMonths,12)*LoanAmount*InterestRate/12+(12-MIN(IOPeriodMonths,12))"
        "*-PMT(InterestRate/12,AmortYears*12,LoanAmount)"
    )
    assert ws["E24"].number_format == L.MONEY and ws["E32"].number_format == L.RATIO
    assert ws["E29"].number_format == L.PERCENT


def test_workbook_properties_and_atomic_write(packages, tmp_path: Path) -> None:
    target = write_workbook(packages["deal_001"], tmp_path / "sub" / "x.xlsx")
    assert not list(tmp_path.rglob("*.tmp"))
    props = openpyxl.load_workbook(target).properties
    expected = packages["deal_001"].created_at.replace(tzinfo=None, microsecond=0)
    assert (
        props.created == expected and props.modified == expected and props.creator == "cre-screen"
    )


def test_money_cells_hold_the_exact_cent_value(packages, deal3) -> None:
    ws, pkg = deal3[L.RENT_ROLL], packages["deal_003"]
    for offset, u in enumerate(pkg.rent_roll.units):
        if u.in_place_rent is not None:
            assert D(str(ws[f"G{offset + 2}"].value)) == u.in_place_rent


# ---------- limits and degenerate inputs ----------


def small_package(*, units=None, lines=None, with_pf=True) -> DealPackage:
    rows = units if units is not None else [unit("1", "1000.00", market="1100.00")]
    rr = roll(rows, in_place_total="1000.00")
    pf = proforma({"GROSS_POTENTIAL_RENT": "12000.00"}) if with_pf else None
    return DealPackage(
        deal_id="deal_small", run_id="r", model_id="m", rent_roll=rr,
        t12=t12(lines) if lines is not None else None, proforma=pf, findings=[], gate_log=[],
        final_state="READY", created_at=NOW,
    )  # fmt: skip


def test_more_than_2000_units_is_refused() -> None:
    units = [unit(str(i), "1.00") for i in range(2001)]
    with pytest.raises(ExportError, match="2,000"):
        build_workbook(small_package(units=units))


def test_exactly_2000_units_fits() -> None:
    ws = build_workbook(small_package(units=[unit(str(i), "1.00") for i in range(2000)]))[
        L.RENT_ROLL
    ]
    assert ws["A2001"].value == "1999" and ws.tables["RentRollTable"].ref == "A1:N2001"


def test_more_than_200_t12_lines_is_refused() -> None:
    lines = [line(f"L{i}", "MARKETING") for i in range(201)]
    with pytest.raises(ExportError, match="200"):
        build_workbook(small_package(lines=lines))


def test_a_deal_without_t12_or_pro_forma_still_gets_every_formula() -> None:
    wb = build_workbook(small_package(lines=None, with_pf=False))
    assert wb[L.ASSUMPTIONS]["B2"].value is None
    assert wb[L.ASSUMPTIONS]["A21"].value is None
    assert wb[L.T12]["A2"].value is None and wb[L.T12]["E1"].value == "M01"
    assert wb[L.PROFORMA]["E24"].value == "=E11-E23"
    assert wb[L.README]["B11"].value == "none"


def test_empty_rent_roll_has_no_table_and_still_builds() -> None:
    wb = build_workbook(small_package(units=[]))
    assert not wb[L.RENT_ROLL].tables


def test_concessions_flow_into_the_effective_rent_inputs() -> None:
    u = unit("1", "1000.00", concessions=[concession("100.00"), concession("25.50")])
    ws = build_workbook(small_package(units=[u]))[L.RENT_ROLL]
    assert ws["K2"].value == 125.5 and ws["L2"].value == "=G2-K2"


# ---------- uw_math by hand ----------


def hand_package(*, asking: str | None = "1000000.00", units=None) -> DealPackage:
    lines = [
        line("Rent", "GROSS_POTENTIAL_RENT", "10000.00"),
        line("Vacancy", "VACANCY_LOSS", "-1000.00"),
        line("Proceeds", "OTHER_INCOME", ["0.00"] * 11 + ["60000.00"], nonrecurring=True),
        line("Taxes", "REAL_ESTATE_TAXES", "1000.00"),
        line("Insurance", "INSURANCE", "500.00"),
        line("Mgmt", "MANAGEMENT_FEE", "100.00"),
        line("Reserves", "CAPEX_RESERVES", "50.00"),
    ]
    default = [unit("1", "9000.00", market="9500.00"), unit("2", None, status="vacant")]
    rr = roll(units if units is not None else default)
    pf = proforma({"GROSS_POTENTIAL_RENT": "150000.00", "MANAGEMENT_FEE": "4000.00"})
    if asking is not None:
        pf = pf.model_copy(update={"asking_price": D(asking)})
    return DealPackage(
        deal_id="d", run_id="r", model_id="m", rent_roll=rr, t12=t12(lines), proforma=pf,
        findings=[], gate_log=[], final_state="READY", created_at=NOW,
    )  # fmt: skip


def test_hand_checked_underwritten_column() -> None:
    uw = compute_uw(hand_package())
    assert uw["B4"] == D("120000.00") and uw["C4"] == D("120000.00") and uw["D4"] == D("150000.00")
    assert uw["B10"] == D("60000.00") and uw["E10"] == D("0"), "one-time income is excluded"
    assert uw["E4"] == D("120000.00") and uw["E6"] == D("-12000.00")
    assert uw["E11"] == D("108000.00")
    assert uw["E14"] == D("6180.00")  # 6,000 grown 3%
    assert uw["E20"] == D("3240.00")  # floor: 3% of 108,000 beats 1,236
    assert uw["E13"] == D("13800.00")  # reassessed fallback 12,000 x 1.15 beats 12,000
    assert uw["E25"] == D("500.00")  # 250 x 2 units
    assert uw["E28"] == D("1000000.00") and uw["E30"] == D("650000.00")


def test_without_an_asking_price_the_price_comes_from_broker_noi() -> None:
    uw = compute_uw(hand_package(asking=None))
    assert uw["D24"] == D("150000.00") - D("4000.00")
    assert uw["E28"] == uw["D24"] / D("0.0625")


def test_reassessed_tax_uses_the_local_ratio_and_rate_when_both_are_known() -> None:
    a = Assumptions(assessment_ratio=D("0.9"), tax_rate=D("0.02"))
    uw = compute_uw(hand_package(), a)
    assert uw["E13"] == D("1000000.00") * D("0.9") * D("0.02")  # 18,000 beats the 12,000 actual


def test_occupancy_and_loss_to_lease_come_from_the_rent_roll() -> None:
    uw = compute_uw(hand_package())
    assert uw["E35"] == D("0.5") and uw["B35"] == uw["E35"]
    assert uw["E37"] == D("500.00") == uw["C37"]


def test_ratio_rows_do_not_divide_by_zero() -> None:
    empty = hand_package(units=[])
    uw = compute_uw(empty)
    assert uw["E35"] == 0 and uw["E25"] == 0
    bare = DealPackage(
        deal_id="d", run_id="r", model_id="m", rent_roll=roll([]), t12=None, proforma=None,
        findings=[], gate_log=[], final_state="READY", created_at=NOW,
    )  # fmt: skip
    zeros = compute_uw(bare)
    assert zeros["E28"] == 0 and zeros["E29"] == 0 and zeros["E32"] == 0 and zeros["E36"] == 0


def test_pmt_matches_the_closed_form_and_handles_a_zero_rate() -> None:
    assert pmt(D("0"), 360, D("360000")) == D("1000")
    monthly = pmt(D("0.065") / 12, 360, D("1000000"))
    assert abs(float(monthly) - 6320.68) < 0.01


@pytest.mark.parametrize(("io", "expected_io_months"), [(0, 0), (6, 6), (12, 12), (24, 12)])
def test_debt_service_blends_interest_only_and_amortizing_months(
    io: int, expected_io_months: int
) -> None:
    loan = D("1000000")
    a = Assumptions(io_period_months=io)
    interest_only = D(expected_io_months) * loan * a.interest_rate / 12
    amortizing = D(12 - expected_io_months) * pmt(a.interest_rate / 12, 360, loan)
    assert debt_service(loan, a) == interest_only + amortizing
    if io >= 12:
        assert debt_service(loan, a) == loan * a.interest_rate


def test_layout_constants_agree_with_the_spec() -> None:
    assert L.REVENUE_ROWS["GROSS_POTENTIAL_RENT"] == 4 and L.REVENUE_ROWS["OTHER_INCOME"] == 10
    assert L.OPEX_ROWS["REAL_ESTATE_TAXES"] == 13 and L.OPEX_ROWS["MARKETING"] == 22
    assert (L.RR_FIRST, L.RR_LAST, L.T12_FIRST, L.T12_LAST, L.PF_FIRST, L.PF_LAST) == (
        2,
        2001,
        2,
        201,
        21,
        40,
    )
    assert date(2026, 1, 1).year == 2026 and cite().page == 1
