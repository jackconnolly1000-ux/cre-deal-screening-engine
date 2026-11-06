from __future__ import annotations

from pathlib import Path

import pytest
from reportlab import rl_config
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

from cre_screen.config import load_config
from cre_screen.errors import UnsupportedFileError
from cre_screen.ingest.pdf_tables import extract_pdf_table
from cre_screen.ingest.render import draw_boxes, page_count, rasterize_pdf, render_page_png
from cre_screen.ingest.router import detect_format

# (header, x of left edge, alignment, width)
COLUMNS = [
    ("Unit", 40, "l", 60),
    ("Type", 110, "l", 60),
    ("Status", 180, "l", 80),
    ("Rent", 270, "r", 70),
    ("Market Rent", 350, "r", 80),
]
HEADER_Y = 520
ROWS = [
    # page 1
    [
        ("101", "1x1", "Occupied", "$1,250.00", "$1,300.00"),
        ("102", "2x1", "Vacant", "", "$1,700.00"),
    ],
    # page 2
    [
        ("201", "1x1", "Occupied", "1,260.00", "1,300.00"),
        ("202", "Studio", "Notice", "950.00", "995.00"),
    ],
]


def draw_cell(c: canvas.Canvas, text: str, col: tuple, y: float) -> None:
    _name, x, align, width = col
    if not text:
        return
    if align == "r":
        c.drawRightString(x + width - 6, y, text)
    else:
        c.drawString(x + 6, y, text)


@pytest.fixture
def two_page_pdf(tmp_path: Path) -> Path:
    rl_config.invariant = 1
    path = tmp_path / "rent_roll.pdf"
    c = canvas.Canvas(str(path), pagesize=landscape(letter), invariant=1)
    for page_no, rows in enumerate(ROWS, start=1):
        c.setFont("Helvetica", 9)
        if page_no == 1:
            c.drawString(40, 570, "Maple Court Apartments")
            c.drawString(40, 556, "As of 2026-09-30")
        else:
            c.drawString(40, 570, "Maple Court Apartments - continued")
        c.setFont("Helvetica-Bold", 8)
        for col in COLUMNS:
            draw_cell(c, col[0], col, HEADER_Y)
        c.setFont("Helvetica", 8)
        y = HEADER_Y - 20
        for row in rows:
            for col, text in zip(COLUMNS, row, strict=True):
                draw_cell(c, text, col, y)
            y -= 18
        if page_no == 2:
            draw_cell(c, "Total", COLUMNS[0], y)
            draw_cell(c, "$2,210.00", COLUMNS[3], y)
            draw_cell(c, "$5,295.00", COLUMNS[4], y)
            c.drawString(40, y - 30, "* All amounts monthly.")
        c.showPage()
    c.save()
    return path


@pytest.fixture(scope="module")
def rr_cfg():
    return load_config().rr_headers


def test_headers_come_from_page_one_and_merge_multi_word_phrases(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    assert table.headers == ["Unit", "Type", "Status", "Rent", "Market Rent"]
    assert table.page_count == 2
    assert table.source_format == "pdf_text"
    assert set(table.header_texts_by_page) == {1, 2}


def test_preamble_is_the_text_above_the_first_header(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    assert table.preamble_text.splitlines() == ["Maple Court Apartments", "As of 2026-09-30"]


def test_repeated_header_on_page_two_is_a_header_row(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    headers = [r for r in table.rows if r.kind == "header"]
    assert [(r.page) for r in headers] == [1, 2]


def test_unit_rows_are_assigned_to_the_right_columns_across_pages(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    units = [r for r in table.rows if r.kind == "unit"]
    assert [r.page for r in units] == [1, 1, 2, 2]
    got = [{h: c.text for h, c in r.cells.items()} for r in units]
    assert got[0] == {
        "Unit": "101",
        "Type": "1x1",
        "Status": "Occupied",
        "Rent": "$1,250.00",
        "Market Rent": "$1,300.00",
    }
    assert "Rent" not in got[1]  # blank cell is omitted
    assert got[1]["Market Rent"] == "$1,700.00"
    assert got[3] == {
        "Unit": "202",
        "Type": "Studio",
        "Status": "Notice",
        "Rent": "950.00",
        "Market Rent": "995.00",
    }


def test_total_and_footnote_rows_are_classified(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    (total,) = [r for r in table.rows if r.kind == "subtotal"]
    assert total.cells["Unit"].text == "Total"
    assert total.cells["Rent"].text == "$2,210.00"
    assert total.cells["Market Rent"].text == "$5,295.00"
    notes = [r.cells["Unit"].text for r in table.rows if r.kind == "footnote"]
    assert len(notes) == 1 and notes[0].startswith("*")


def test_every_pdf_cell_has_a_bbox_in_page_coordinates(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    unit = next(r for r in table.rows if r.kind == "unit")
    for cell in unit.cells.values():
        assert cell.cell is None
        x0, top, x1, bottom = cell.bbox  # type: ignore[misc]
        assert 0 <= x0 < x1 and 0 <= top < bottom
    # top is measured from the page top: baseline y=500 on a 612 pt page is about 104 pt down
    assert 95 < unit.cells["Unit"].bbox[1] < 110  # type: ignore[index]


def test_row_indexes_are_sequential(two_page_pdf, rr_cfg) -> None:
    table = extract_pdf_table(two_page_pdf, rr_cfg)
    assert [r.row_index for r in table.rows] == list(range(len(table.rows)))


def test_extraction_cache_round_trips_and_is_reused(two_page_pdf, rr_cfg, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    first = extract_pdf_table(two_page_pdf, rr_cfg, cache_dir=cache)
    assert len(list(cache.glob("*.json"))) == 1
    second = extract_pdf_table(two_page_pdf, rr_cfg, cache_dir=cache)
    assert second == first


def test_cache_entry_is_ignored_when_header_config_changes(
    two_page_pdf, rr_cfg, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    extract_pdf_table(two_page_pdf, rr_cfg, cache_dir=cache)
    changed = rr_cfg.model_copy(update={"fuzzy_min_score": 95})
    entry = next(cache.glob("*.json"))
    before = entry.read_text(encoding="utf-8")
    extract_pdf_table(two_page_pdf, changed, cache_dir=cache)
    assert entry.read_text(encoding="utf-8") != before


# ---------- router and render ----------


def test_text_pdf_is_detected(two_page_pdf) -> None:
    assert detect_format(two_page_pdf) == "pdf_text"


def test_image_only_pdf_is_detected_as_scanned(two_page_pdf, tmp_path: Path) -> None:
    scanned = tmp_path / "scan.pdf"
    rasterize_pdf(two_page_pdf, scanned, dpi=72)
    assert detect_format(scanned) == "pdf_scanned"


def test_xlsx_is_detected_and_other_extensions_rejected(tmp_path: Path) -> None:
    assert detect_format(tmp_path / "x.xlsx") == "xlsx"
    with pytest.raises(UnsupportedFileError):
        detect_format(tmp_path / "x.xls")


def test_render_page_count_png_and_boxes(two_page_pdf) -> None:
    assert page_count(two_page_pdf) == 2
    png = render_page_png(two_page_pdf, 1, dpi=72)
    assert png.startswith(b"\x89PNG")
    boxed = draw_boxes(png, [(40.0, 100.0, 200.0, 120.0)], scale=1.0)
    assert boxed.startswith(b"\x89PNG") and boxed != png
