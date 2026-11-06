from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from cre_screen.config import load_config
from cre_screen.errors import UnsupportedFileError
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.ingest.excel_tables import extract_excel_table
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.reconcile.checks.rr_footing import check
from cre_screen.reconcile.context import CheckContext
from tests.fakes import FakeLLMClient

D = Decimal
CFG = load_config()


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rent Roll"
    ws["A1"] = "Birch Court Apartments"
    ws["A2"] = "As of 2026-09-30"
    headers = [
        "Unit",
        "Floorplan",
        "Status",
        "Contract Rent",
        "Market Rent",
        "Lease From",
        "Lease To",
    ]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=4, column=c, value=h)
    rows = [
        (101, "1x1", "Occupied", 1250.1, 1300, datetime(2026, 1, 1), datetime(2026, 12, 31)),
        ("102", "2x1", "Vacant", None, 1700, None, None),
        (103, "1x1", "Occupied", 1400, 1400.0, datetime(2026, 3, 1), datetime(2027, 2, 28)),
    ]
    for r, row in enumerate(rows, start=5):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v)
    ws.cell(row=9, column=1, value="Total")
    ws.cell(row=9, column=4, value=2650.1)
    ws.cell(row=9, column=5, value=4400)
    # Values, not formulas: openpyxl returns None for formula cells in files Excel never opened.
    other = wb.create_sheet("Other")
    other["A1"] = "ignore me"
    path = tmp_path / "rent_roll.xlsx"
    wb.save(path)
    return path


def test_header_row_is_found_below_title_rows(workbook: Path) -> None:
    table = extract_excel_table(workbook, CFG.rr_headers)
    assert table.source_format == "xlsx"
    assert table.headers[0] == "Unit" and len(table.headers) == 7
    assert table.preamble_text.splitlines() == ["Birch Court Apartments", "As of 2026-09-30"]


def test_cells_carry_addresses_and_numbers_are_text_without_float_noise(workbook: Path) -> None:
    table = extract_excel_table(workbook, CFG.rr_headers)
    units = [r for r in table.rows if r.kind == "unit"]
    assert [r.cells["Unit"].text for r in units] == ["101", "102", "103"]
    assert units[0].cells["Contract Rent"].text == "1250.1"
    assert units[0].cells["Contract Rent"].cell == "D5"
    assert units[0].sheet == "Rent Roll" and units[0].page is None
    assert units[2].cells["Market Rent"].text == "1400"
    assert units[0].cells["Lease From"].text == "2026-01-01"
    assert "Contract Rent" not in units[1].cells  # blank cell omitted


def test_total_row_is_a_subtotal_and_blank_rows_are_blank(workbook: Path) -> None:
    table = extract_excel_table(workbook, CFG.rr_headers)
    kinds = [r.kind for r in table.rows]
    assert kinds.count("subtotal") == 1
    assert "blank" in kinds


def test_full_extraction_from_excel_foots_and_cites_cells(workbook: Path) -> None:
    fake = FakeLLMClient()
    table = extract_excel_table(workbook, CFG.rr_headers)
    header_map, _ = map_headers(table.headers, [], CFG.rr_headers, fake)
    rr = build_rent_roll(table, header_map, CFG, llm=fake)
    assert fake.calls == []
    assert [u.unit_id for u in rr.units] == ["101", "102", "103"]
    assert rr.units[0].in_place_rent == D("1250.10")
    assert rr.units[1].in_place_rent is None and rr.units[1].status == "vacant"
    assert rr.units[0].lease_end == date(2026, 12, 31)
    assert rr.as_of_date == date(2026, 9, 30)
    cite = rr.units[0].citations["in_place_rent"]
    assert (cite.sheet, cite.cell, cite.provenance, cite.page) == ("Rent Roll", "D5", "excel", None)
    assert rr.stated_totals == {"in_place_rent": D("2650.10"), "market_rent": D("4400.00")}
    ctx = CheckContext(
        rent_roll=rr, t12=None, proforma=None, thresholds=CFG.thresholds, as_of=date(2026, 9, 30)
    )
    assert check(ctx) == []


def test_named_sheet_is_used_and_missing_sheet_is_an_error(workbook: Path) -> None:
    with pytest.raises(UnsupportedFileError, match="no header row"):
        extract_excel_table(workbook, CFG.rr_headers, sheet="Other")
    with pytest.raises(UnsupportedFileError, match="not found"):
        extract_excel_table(workbook, CFG.rr_headers, sheet="Nope")


def test_unparseable_value_becomes_a_flag_and_warning_not_a_crash(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    for c, h in enumerate(["Unit", "Status", "Rent"], start=1):
        ws.cell(row=1, column=c, value=h)
    for c, v in enumerate(["101", "Occupied", "call office"], start=1):
        ws.cell(row=2, column=c, value=v)
    path = tmp_path / "rr.xlsx"
    wb.save(path)
    fake = FakeLLMClient()
    table = extract_excel_table(path, CFG.rr_headers)
    header_map, _ = map_headers(table.headers, [], CFG.rr_headers, fake)
    rr = build_rent_roll(table, header_map, CFG, llm=fake)
    (u,) = rr.units
    assert u.in_place_rent is None and "unparseable_in_place_rent" in u.audit_flags
    assert any("could not parse in_place_rent" in w for w in rr.extraction_warnings)
    assert "no as-of date found" in rr.extraction_warnings
