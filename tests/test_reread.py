from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from cre_screen.config import load_config
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.ingest.pdf_tables import extract_pdf_table
from cre_screen.ingest.reread import ExcelRereader, PdfRereader, make_rereader
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.reconcile.reread import compare_money, localize
from cre_screen.schemas.citation import Citation
from tests.builders import D, roll, subtotal, unit
from tests.fakes import FakeLLMClient

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
CFG = load_config()
TOLERANCE = D("1.00")


# ---------- pure parts ----------


def test_localize_returns_the_whole_table_when_nothing_is_localizable() -> None:
    rr = roll([unit("1"), unit("2")], in_place_total="5.00")
    assert [u.unit_id for u in localize(rr, TOLERANCE)] == ["1", "2"]


def test_localize_picks_the_smallest_failing_in_place_subtotal() -> None:
    units = [unit(str(i), "100.00") for i in range(1, 7)]
    rr = roll(
        units,
        subtotals=[
            subtotal("ok", "200.00", ["1", "2"]),
            subtotal("big", "1.00", ["1", "2", "3", "4"]),
            subtotal("small", "1.00", ["5", "6"]),
            subtotal("market", "1.00", ["1"], column="market_rent"),
            subtotal("grand", "1.00", ["1", "2", "3", "4", "5", "6"], scope="grand_total"),
        ],
    )
    assert [u.unit_id for u in localize(rr, TOLERANCE)] == ["5", "6"]


def test_localize_respects_the_tolerance() -> None:
    rr = roll([unit("1", "100.00")], subtotals=[subtotal("p", "101.00", ["1"])])
    assert [u.unit_id for u in localize(rr, TOLERANCE)] == ["1"]  # whole table, nothing failed
    assert len(localize(rr, D("0.50"))) == 1


def test_compare_money_agrees_only_when_every_field_matches_to_the_cent() -> None:
    units = [unit("1", "100.00", market="120.00"), unit("2", "200.00")]
    same = [
        {"in_place_rent": D("100.00"), "market_rent": D("120.00")},
        {"in_place_rent": D("200.00")},
    ]
    assert compare_money(units, same).agrees
    off_by_a_cent = [{"in_place_rent": D("100.01")}, {"in_place_rent": D("200.00")}]
    result = compare_money(units, off_by_a_cent)
    assert not result.agrees and result.disagreeing_unit_ids == ["1"]


def test_compare_money_treats_none_vs_value_as_disagreement() -> None:
    assert not compare_money([unit("1", "100.00")], [{"in_place_rent": None}]).agrees
    assert compare_money([unit("1", None, status="vacant")], [{"in_place_rent": None}]).agrees


def test_compare_money_ignores_fields_the_reread_did_not_cover() -> None:
    assert compare_money(
        [unit("1", "100.00", market="5.00")], [{"in_place_rent": D("100.00")}]
    ).agrees


def test_compare_money_needs_one_entry_per_unit() -> None:
    with pytest.raises(ValueError, match="one entry per unit"):
        compare_money([unit("1")], [])


# ---------- the text-layer PDF re-reader on a real synthetic rent roll ----------


@pytest.fixture(scope="module")
def deal_001():
    path = DATA / "deal_001" / "rent_roll.pdf"
    table = extract_pdf_table(path, CFG.rr_headers)
    fake = FakeLLMClient()
    header_map, _ = map_headers(table.headers, [], CFG.rr_headers, fake)
    return path, build_rent_roll(table, header_map, CFG, llm=fake)


def test_pdf_reread_agrees_with_a_correct_first_read(deal_001) -> None:
    path, rr = deal_001
    result = PdfRereader(path, rr)(rr.units)
    assert result.agrees and result.disagreeing_unit_ids == []


def test_pdf_reread_catches_a_value_the_first_read_got_wrong(deal_001) -> None:
    path, rr = deal_001
    wrong = [u.model_copy(deep=True) for u in rr.units]
    wrong[10].in_place_rent = wrong[10].in_place_rent + D("1.00")
    wrong[20].market_rent = D("1.00")
    result = PdfRereader(path, rr)(wrong)
    assert not result.agrees
    assert result.disagreeing_unit_ids == [wrong[10].unit_id, wrong[20].unit_id]


def test_pdf_reread_of_a_subset_only_reads_that_block(deal_001) -> None:
    path, rr = deal_001
    assert PdfRereader(path, rr)(rr.units[30:35]).agrees


def test_pdf_reread_needs_a_position_for_each_unit(deal_001) -> None:
    path, rr = deal_001
    blind = unit("X1")
    with pytest.raises(ValueError, match="no page position"):
        PdfRereader(path, rr)([blind])


def test_make_rereader_picks_the_reader_by_source_format(deal_001, tmp_path: Path) -> None:
    path, rr = deal_001
    assert isinstance(make_rereader(path, rr), PdfRereader)
    xlsx = rr.model_copy(update={"source_format": "xlsx"})
    assert isinstance(make_rereader(tmp_path / "x.xlsx", xlsx), ExcelRereader)
    assert make_rereader(path, rr.model_copy(update={"source_format": "pdf_scanned"})) is None


# ---------- the Excel re-reader ----------


def excel_units(tmp_path: Path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rent Roll"
    ws["D5"], ws["E5"], ws["D6"] = 1250.1, 1300, 900
    path = tmp_path / "rent_roll.xlsx"
    wb.save(path)

    def with_cells(
        unit_id: str, rent: str, rent_cell: str, market: str | None = None, market_cell=None
    ):
        u = unit(unit_id, rent, market=market)
        u.citations["in_place_rent"] = Citation(
            source_file="rent_roll.xlsx",
            sheet="Rent Roll",
            cell=rent_cell,
            row_index=0,
            raw_text=rent,
            provenance="excel",
        )
        if market_cell:
            u.citations["market_rent"] = Citation(
                source_file="rent_roll.xlsx",
                sheet="Rent Roll",
                cell=market_cell,
                row_index=0,
                raw_text=market or "",
                provenance="excel",
            )
        return u

    return path, with_cells


def test_excel_reread_agrees_when_the_cells_still_hold_the_extracted_values(tmp_path: Path) -> None:
    path, make = excel_units(tmp_path)
    units = [make("1", "1250.10", "D5", "1300.00", "E5"), make("2", "900.00", "D6")]
    assert ExcelRereader(path)(units).agrees


def test_excel_reread_flags_units_whose_cell_disagrees(tmp_path: Path) -> None:
    path, make = excel_units(tmp_path)
    units = [make("1", "1250.11", "D5"), make("2", "900.00", "D6")]
    result = ExcelRereader(path)(units)
    assert not result.agrees and result.disagreeing_unit_ids == ["1"]
