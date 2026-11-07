from __future__ import annotations

import base64
import io
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from cre_screen.config import load_config
from cre_screen.extract.rent_roll import build_rent_roll
from cre_screen.ingest.render import images_to_pdf, page_image
from cre_screen.ingest.reread import VisionRereader, make_rereader
from cre_screen.ingest.router import detect_format
from cre_screen.mapping.header_mapper import map_headers
from cre_screen.mapping.prompts import SYSTEM_LINE
from cre_screen.mapping.vision import (
    CAREFUL_SUFFIX,
    VisionError,
    page_rows,
    transcribe_page,
    vision_table,
)
from cre_screen.schemas.mapping import VisionRow, VisionTablePage
from tests.fakes import FakeLLMClient

D = Decimal
CFG = load_config()
HEADERS = ["Unit", "Type", "Status", "Contract Rent", "Market Rent"]


def jpeg(color: int, size: tuple[int, int] = (200, 120)) -> bytes:
    out = io.BytesIO()
    Image.new("L", size, color).save(out, format="JPEG", quality=70)
    return out.getvalue()


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    """A two-page image-only PDF, like a scan."""
    path = tmp_path / "rent_roll.pdf"
    images_to_pdf([jpeg(250), jpeg(240)], [(200.0, 120.0), (200.0, 120.0)], path)
    return path


def row(kind: str, *cells: str, headers: list[str] | None = None) -> VisionRow:
    keys = headers or HEADERS
    return VisionRow(cells=dict(zip(keys, cells, strict=False)), row_kind=kind)  # type: ignore[arg-type]


PAGE_1 = VisionTablePage(
    headers=HEADERS,
    rows=[
        row("other", "Maple Court Apartments"),
        row("other", "As of 2026-10-31"),
        row("header", *HEADERS),
        row("unit", "101", "1x1", "Occupied", "$1,250.00", "$1,300.00"),
        row("unit", "102", "2x1", "Vacant", "", "$1,700.00"),
    ],
)
PAGE_2 = VisionTablePage(
    headers=HEADERS,
    rows=[
        row("header", *HEADERS),
        row("unit", "201", "1x1", "Occupied", "1,260.00", "1,300.00"),
        row("subtotal", "Total", "", "", "$2,510.00", "$4,300.00"),
        row("footnote", "* amounts monthly"),
    ],
)


class PageByPage:
    """Returns the next scripted page for each vision_page call and records every request."""

    def __init__(self, *pages: VisionTablePage) -> None:
        self.pages = list(pages)
        self.requests: list[dict] = []

    def structured(self, *, task, response_model, system, user_content, model=None):
        assert task == "vision_page" and response_model is VisionTablePage
        self.requests.append({"system": system, "content": user_content})
        return self.pages.pop(0)


# ---------- what the model is shown ----------


def test_a_scan_is_detected_as_scanned(scanned_pdf: Path) -> None:
    assert detect_format(scanned_pdf) == "pdf_scanned"


def test_the_embedded_scan_is_sent_verbatim_not_re_rendered(scanned_pdf: Path) -> None:
    data, media_type = page_image(scanned_pdf, 1)
    assert media_type == "image/jpeg" and data[:2] == b"\xff\xd8"
    assert data == page_image(scanned_pdf, 1)[0], "stable across calls"


def test_a_text_layer_page_falls_back_to_a_rendered_png() -> None:
    pdf = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "deal_001" / "rent_roll.pdf"
    data, media_type = page_image(pdf, 1)
    assert media_type == "image/png" and data.startswith(b"\x89PNG")


def test_each_page_is_its_own_call_with_an_image_block_then_the_prompt(scanned_pdf: Path) -> None:
    llm = PageByPage(PAGE_1, PAGE_2)
    vision_table(scanned_pdf, llm)
    assert len(llm.requests) == 2
    for number, request in enumerate(llm.requests, start=1):
        image, prompt = request["content"]
        assert image["type"] == "image" and image["source"]["type"] == "base64"
        assert image["source"]["media_type"] == "image/jpeg"
        sent = base64.b64decode(image["source"]["data"])
        assert sent == page_image(scanned_pdf, number)[0]
        assert prompt["type"] == "text" and prompt["text"].startswith("This image is one page")
        assert request["system"] == SYSTEM_LINE


def test_careful_reading_uses_a_different_system_prompt_so_it_caches_separately(
    scanned_pdf: Path,
) -> None:
    llm = PageByPage(PAGE_1, PAGE_1)
    transcribe_page(scanned_pdf, 1, llm)
    transcribe_page(scanned_pdf, 1, llm, careful=True)
    first, second = (r["system"] for r in llm.requests)
    assert first == SYSTEM_LINE and second == SYSTEM_LINE + CAREFUL_SUFFIX != first


# ---------- the table built from the transcription ----------


def test_pages_become_a_raw_table_with_kinds_pages_and_no_boxes(scanned_pdf: Path) -> None:
    table = vision_table(scanned_pdf, PageByPage(PAGE_1, PAGE_2))
    assert table.source_format == "pdf_scanned" and table.page_count == 2
    assert table.headers == HEADERS
    assert table.header_texts_by_page == {1: HEADERS, 2: HEADERS}
    assert [(r.page, r.kind) for r in table.rows] == [
        (1, "header"),
        (1, "unit"),
        (1, "unit"),
        (2, "header"),
        (2, "unit"),
        (2, "subtotal"),
        (2, "footnote"),
    ]  # fmt: skip
    assert [r.row_index for r in table.rows] == list(range(7))
    assert all(c.bbox is None and c.cell is None for r in table.rows for c in r.cells.values())


def test_title_lines_above_the_table_become_the_preamble(scanned_pdf: Path) -> None:
    table = vision_table(scanned_pdf, PageByPage(PAGE_1, PAGE_2))
    assert table.preamble_text.splitlines() == ["Maple Court Apartments", "As of 2026-10-31"]


def test_blank_cells_are_omitted_and_text_is_kept_exactly_as_printed(scanned_pdf: Path) -> None:
    table = vision_table(scanned_pdf, PageByPage(PAGE_1, PAGE_2))
    vacant = next(r for r in table.rows if r.cells.get("Unit") and r.cells["Unit"].text == "102")
    assert "Contract Rent" not in vacant.cells and vacant.cells["Market Rent"].text == "$1,700.00"


def test_page_headers_that_differ_in_ocr_are_mapped_by_column_position() -> None:
    drifted = VisionTablePage(
        headers=["Unit", "Type", "Status", "Contract  Rent", "Mkt Rent"],
        rows=[
            VisionRow(
                cells={"Unit": "201", "Contract  Rent": "1,260.00", "Mkt Rent": "1,300.00"},
                row_kind="unit",
            )
        ],
    )
    (_, cells, _raw) = page_rows(drifted, HEADERS)[0]
    assert cells == {"Unit": "201", "Contract Rent": "1,260.00", "Market Rent": "1,300.00"}


def test_when_column_counts_differ_headers_are_matched_by_text_and_strays_dropped() -> None:
    shorter = VisionTablePage(
        headers=["Unit", "contract rent", "Odd"],
        rows=[VisionRow(cells={"Unit": "9", "contract rent": "5.00", "Odd": "x"}, row_kind="unit")],
    )
    (_, cells, _raw) = page_rows(shorter, HEADERS)[0]
    assert cells == {"Unit": "9", "Contract Rent": "5.00"}


def test_other_row_kinds_become_footnotes() -> None:
    page = VisionTablePage(headers=HEADERS, rows=[row("other", "stray text")])
    assert page_rows(page, HEADERS)[0][0] == "footnote"


def test_a_scan_with_no_readable_headers_is_an_error(scanned_pdf: Path) -> None:
    empty = VisionTablePage(headers=[], rows=[])
    with pytest.raises(VisionError, match="no table headers"):
        vision_table(scanned_pdf, PageByPage(empty, empty))


def test_a_page_without_headers_is_skipped_until_one_has_them(scanned_pdf: Path) -> None:
    table = vision_table(scanned_pdf, PageByPage(VisionTablePage(headers=[], rows=[]), PAGE_2))
    assert table.headers == HEADERS and table.header_texts_by_page == {2: HEADERS}


# ---------- through the extractor: provenance, parsing, citations ----------


@pytest.fixture
def rent_roll(scanned_pdf: Path):
    llm = PageByPage(PAGE_1, PAGE_2)
    table = vision_table(scanned_pdf, llm)
    header_map, _ = map_headers(table.headers, [], CFG.rr_headers, FakeLLMClient())
    return build_rent_roll(table, header_map, CFG, llm=FakeLLMClient())


def test_every_citation_carries_llm_vision_provenance(rent_roll) -> None:
    citations = [c for u in rent_roll.units for c in u.citations.values()]
    citations += list(rent_roll.stated_total_citations.values())
    citations += [s.citation for s in rent_roll.subtotals]
    assert citations and {c.provenance for c in citations} == {"llm_vision"}


def test_citations_have_a_page_and_row_but_no_box_or_cell(rent_roll) -> None:
    first = rent_roll.units[0].citations["in_place_rent"]
    assert first.page == 1 and first.row_index == 1 and first.bbox is None and first.cell is None
    assert first.raw_text == "$1,250.00" and first.source_file == "rent_roll.pdf"
    assert rent_roll.units[2].citations["unit_id"].page == 2
    assert rent_roll.source_format == "pdf_scanned"


def test_the_transcribed_strings_are_parsed_by_code_into_exact_values(rent_roll) -> None:
    assert [u.unit_id for u in rent_roll.units] == ["101", "102", "201"]
    assert [u.in_place_rent for u in rent_roll.units] == [D("1250.00"), None, D("1260.00")]
    assert [u.market_rent for u in rent_roll.units] == [D("1300.00"), D("1700.00"), D("1300.00")]
    assert [u.status for u in rent_roll.units] == ["occupied", "vacant", "occupied"]
    assert all(isinstance(u.in_place_rent, (Decimal, type(None))) for u in rent_roll.units)
    assert rent_roll.stated_totals == {"in_place_rent": D("2510.00"), "market_rent": D("4300.00")}
    assert rent_roll.property_name == "Maple Court Apartments"
    assert rent_roll.as_of_date == date(2026, 10, 31)


def test_unparseable_scanned_text_is_flagged_not_trusted() -> None:
    page = VisionTablePage(
        headers=HEADERS, rows=[row("unit", "101", "1x1", "Occupied", "12O0.00", "1,300.00")]
    )
    table = vision_table_from(page)
    header_map, _ = map_headers(table.headers, [], CFG.rr_headers, FakeLLMClient())
    rr = build_rent_roll(table, header_map, CFG, llm=FakeLLMClient())
    assert rr.units[0].in_place_rent is None
    assert "unparseable_in_place_rent" in rr.units[0].audit_flags


def vision_table_from(page: VisionTablePage):
    from cre_screen.ingest.raw import RawCell, RawRow, RawTable

    kinds = {"unit": "unit", "subtotal": "subtotal", "header": "header"}
    rows = [
        RawRow(
            1,
            None,
            i,
            kinds.get(r.row_kind, "footnote"),
            {h: RawCell(t, None, None) for h, t in r.cells.items() if t},
        )  # type: ignore[arg-type]
        for i, r in enumerate(page.rows)
    ]
    return RawTable(
        "x.pdf", "pdf_scanned", list(page.headers), rows, 1, {1: list(page.headers)}, ""
    )


# ---------- the vision re-reader used by the gate ----------


@pytest.fixture
def reread_setup(scanned_pdf: Path, rent_roll):
    return scanned_pdf, rent_roll


def test_a_matching_second_reading_agrees(reread_setup) -> None:
    pdf, rr = reread_setup
    llm = PageByPage(PAGE_1, PAGE_2)
    result = VisionRereader(pdf, rr, llm)(rr.units)
    assert result.agrees and result.disagreeing_unit_ids == []
    assert [r["system"] for r in llm.requests] == [SYSTEM_LINE + CAREFUL_SUFFIX] * 2


def test_only_the_pages_of_the_block_are_re_read(reread_setup) -> None:
    pdf, rr = reread_setup
    llm = PageByPage(PAGE_2)
    result = VisionRereader(pdf, rr, llm)([rr.units[2]])  # unit 201 lives on page 2
    assert result.agrees and len(llm.requests) == 1


def test_a_different_second_reading_names_the_units_that_disagree(reread_setup) -> None:
    pdf, rr = reread_setup
    misread = VisionTablePage(
        headers=HEADERS,
        rows=[
            row("header", *HEADERS),
            row("unit", "201", "1x1", "Occupied", "1,280.00", "1,300.00"),
        ],
    )
    result = VisionRereader(pdf, rr, PageByPage(misread))([rr.units[2]])
    assert not result.agrees and result.disagreeing_unit_ids == ["201"]


def test_a_unit_the_second_reading_cannot_find_counts_as_a_disagreement(reread_setup) -> None:
    pdf, rr = reread_setup
    gone = VisionTablePage(headers=HEADERS, rows=[row("header", *HEADERS)])
    result = VisionRereader(pdf, rr, PageByPage(gone))([rr.units[2]])
    assert not result.agrees and result.disagreeing_unit_ids == ["201"]


def test_make_rereader_builds_a_vision_reader_for_scans_when_a_client_exists(
    scanned_pdf: Path, rent_roll
) -> None:
    assert isinstance(make_rereader(scanned_pdf, rent_roll, FakeLLMClient()), VisionRereader)
    assert make_rereader(scanned_pdf, rent_roll) is None


# ---------- the prompt ----------


def spec_prompt_block(name: str) -> str:
    """The fenced text block under the heading for the template in SPEC section 14."""
    spec = (Path(__file__).resolve().parents[1] / "SPEC.md").read_text(encoding="utf-8")
    start = spec.index(f"**`{name}.md`**")
    fence = spec.index("```text", start) + len("```text")
    return spec[fence : spec.index("```", fence)].strip()


@pytest.mark.parametrize(
    "name", ["vision_page", "vision_t12", "header_map", "coa_map", "concession"]
)
def test_prompt_templates_match_spec_section_14_exactly(name: str) -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "src" / "cre_screen" / "mapping" / "prompts" / f"{name}.md"
    ).read_text(encoding="utf-8")  # fmt: skip
    assert template.strip() == spec_prompt_block(name)


def test_the_vision_prompt_asks_for_title_lines_as_other_rows() -> None:
    from cre_screen.mapping.prompts import render_prompt

    text = render_prompt("vision_page")
    assert "title line" in text and "As of" in text and "row_kind other" in text
