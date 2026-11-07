"""Real-world file shapes: wrapped cells, headerless label columns, shared pages, brochures,
and the fallback to the vision reader (SPEC 7.6). Everything here is offline."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cre_screen.config import load_config
from cre_screen.errors import HeaderMapError, T12ExtractError
from cre_screen.extract.t12 import build_t12, period_end_of
from cre_screen.ingest.pdf_tables import extract_pdf_t12_table, extract_pdf_table
from cre_screen.ingest.raw import RawTable
from cre_screen.ingest.routing import route_pdf
from cre_screen.ingest.unwrap import join_fragments, unwrap_words
from cre_screen.pipeline import run_deal
from cre_screen.read_inputs import read_rent_roll, read_t12
from cre_screen.schemas.mapping import VisionRow, VisionTablePage
from tests.fakes import CannedCoaLLM
from tests.pdf_makers import (
    both_on_one_page,
    filler,
    pages_pdf,
    rent_roll_page,
    t12_page,
    text_pdf,
)

D = Decimal
CFG = load_config()
AS_OF = date(2026, 4, 3)


def word(text: str, x0: float, top: float, width: float = 20.0, height: float = 8.0):
    return {"text": text, "x0": x0, "x1": x0 + width, "top": top, "bottom": top + height}


def units_of(table: RawTable) -> list[dict[str, str]]:
    return [{h: c.text for h, c in r.cells.items()} for r in table.rows if r.kind == "unit"]


# ---------- (a) wrapped cells ----------


@pytest.mark.parametrize(
    ("upper", "lower", "joined"),
    [
        ("Uni", "t", "Unit"),
        ("04/03/2", "6", "04/03/26"),
        ("$110,95", "6", "$110,956"),
        ("Lease", "Start", "Lease Start"),
        ("Current", "Rent", "Current Rent"),
        ("1,2", "50.00", "1,250.00"),
    ],
)
def test_fragments_are_joined_without_a_space_only_inside_a_word_or_number(
    upper: str, lower: str, joined: str
) -> None:
    assert join_fragments(upper, lower) == joined


def test_a_stack_of_fragments_in_one_column_becomes_one_word_on_the_main_line() -> None:
    words = [
        word("04/03/2", 420, 94),
        word("101", 50, 100),
        word("Active", 200, 100),
        word("6", 420, 106),
    ]
    merged = {w["text"]: w for w in unwrap_words(words)}
    assert set(merged) == {"101", "Active", "04/03/26"}
    date_word = merged["04/03/26"]
    assert date_word["top"] == 100, "it sits on the row's main line"
    assert (date_word["box_top"], date_word["box_bottom"]) == (94, 114), "the box covers both"


def test_ordinary_lines_pass_through_unchanged() -> None:
    words = [word("a", 50, 100), word("b", 100, 118), word("c", 150, 136)]
    assert unwrap_words(words) == words


def test_wrapped_headers_dates_and_money_are_rejoined(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "rent_roll.pdf", [rent_roll_page(wrapped=True)])
    plain = extract_pdf_table(pdf, CFG.rr_headers)
    assert "Unit" not in plain.headers, "the plain reader sees 'Uni' and 't' apart"
    healed = extract_pdf_table(pdf, CFG.rr_headers, unwrap=True)
    assert healed.headers[0] == "Unit" and "Current Rent" in healed.headers
    rows = units_of(healed)
    assert [r["Unit"] for r in rows][:2] == ["101", "102"]
    assert rows[0]["Last Pmt"] == "04/03/26"
    assert rows[0]["Current Rent"] == "$650"


def test_wrapped_cells_keep_a_box_that_covers_both_fragments(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "rent_roll.pdf", [rent_roll_page(wrapped=True)])
    table = extract_pdf_table(pdf, CFG.rr_headers, unwrap=True)
    row = next(r for r in table.rows if r.kind == "unit")
    box = row.cells["Last Pmt"].bbox
    assert box is not None and box[3] - box[1] >= 12, "the citation spans both printed fragments"


def test_a_wrapped_money_total_in_a_t12_is_rejoined(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "t12.pdf", [t12_page(wrapped_total=True)])
    table = extract_pdf_t12_table(pdf, CFG.coa, unwrap=True)
    first = next(r for r in table.rows if r.kind == "line")
    assert first.cells["Total"].text == "$12,345"


def test_a_clean_table_reads_the_same_with_or_without_unwrapping(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "rent_roll.pdf", [rent_roll_page()])
    plain = extract_pdf_table(pdf, CFG.rr_headers)
    healed = extract_pdf_table(pdf, CFG.rr_headers, unwrap=True)
    assert units_of(plain) == units_of(healed) and plain.headers == healed.headers


# ---------- (b) headerless label column and month-only labels ----------


def test_a_t12_without_a_header_over_the_line_items_gets_one_from_the_content(
    tmp_path: Path,
) -> None:
    pdf = pages_pdf(tmp_path / "t12.pdf", [t12_page()])
    table = extract_pdf_t12_table(pdf, CFG.coa)
    assert table.headers[0] == "Line Item" and table.headers[1] == "Jan"
    lines = [r.cells["Line Item"].text for r in table.rows if r.kind == "line"]
    assert lines[:3] == ["Rent Income", "Utilities", "Landscaping"]
    subtotal = next(r for r in table.rows if r.kind == "subtotal")
    assert subtotal.cells["Line Item"].text == "Total Income"


def test_a_printed_label_header_is_kept_as_it_is(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "t12.pdf", [t12_page(label_header="Account")])
    table = extract_pdf_t12_table(pdf, CFG.coa)
    assert table.headers[0] == "Account" and "Line Item" not in table.headers


def test_month_names_without_a_year_take_it_from_the_title(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "t12.pdf", [t12_page()])
    statement = build_t12(extract_pdf_t12_table(pdf, CFG.coa), CFG, llm=CannedCoaLLM())
    assert statement.period_end == date(2025, 12, 31)
    assert statement.month_labels[0] == "Jan" and len(statement.lines) == 8
    first = statement.lines[0]
    assert first.bucket == "GROSS_POTENTIAL_RENT" or first.label == "Rent Income"
    assert first.stated_total == D("12345.00")


def test_month_names_without_any_year_are_rejected() -> None:
    with pytest.raises(T12ExtractError, match="no year"):
        period_end_of("Dec", "T-12 FINANCIAL STATEMENT")


def test_the_year_is_matched_to_the_last_month_not_to_another_date() -> None:
    title = "OFFERING MEMORANDUM APRIL 2026 - T-12 JANUARY THROUGH DECEMBER 2025"
    assert period_end_of("Dec", title) == date(2025, 12, 31)


# ---------- (c) one page holding both tables ----------


def test_a_page_with_both_tables_is_split_into_two_regions(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "flyer.pdf", [both_on_one_page()])
    routing = route_pdf(pdf, CFG.rr_headers)
    assert routing.has_rent_roll and routing.has_t12
    (rr,), (t12,) = routing.rent_roll or [], routing.t12 or []
    assert rr.page == t12.page == 1 and rr.bottom == t12.top and 0 < rr.bottom < 792


def test_each_reader_sees_only_its_own_slice_of_the_shared_page(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "flyer.pdf", [both_on_one_page()])
    routing = route_pdf(pdf, CFG.rr_headers)
    rr_table = extract_pdf_table(pdf, CFG.rr_headers, regions=routing.rent_roll)
    assert len(units_of(rr_table)) == 6
    assert not [r for r in rr_table.rows if r.kind == "subtotal"], "T-12 totals stay out"
    t12_table = extract_pdf_t12_table(pdf, CFG.coa, regions=routing.t12)
    assert [r.cells["Line Item"].text for r in t12_table.rows if r.kind == "line"][:2] == [
        "Rent Income",
        "Utilities",
    ]


def test_one_file_in_the_rent_roll_slot_serves_both_tables(tmp_path: Path) -> None:
    deal = tmp_path / "flyer_deal"
    deal.mkdir()
    pages_pdf(deal / "rent_roll.pdf", [both_on_one_page()])
    pkg = run_deal(deal, llm=CannedCoaLLM(), cfg=CFG, as_of=AS_OF, out_dir=tmp_path / "out")
    assert len(pkg.rent_roll.units) == 6
    assert pkg.t12 is not None and len(pkg.t12.lines) == 6
    assert pkg.t12.period_end == date(2025, 12, 31)


def test_the_same_one_page_file_in_both_slots_is_accepted(tmp_path: Path) -> None:
    deal = tmp_path / "twice"
    deal.mkdir()
    for name in ("rent_roll.pdf", "t12.pdf"):
        pages_pdf(deal / name, [both_on_one_page()])
    pkg = run_deal(deal, llm=CannedCoaLLM(), cfg=CFG, as_of=AS_OF, out_dir=tmp_path / "out")
    assert len(pkg.rent_roll.units) == 6 and pkg.t12 is not None and len(pkg.t12.lines) == 6


# ---------- (d) brochures ----------


def brochure(path: Path) -> Path:
    pages = [
        filler("INVESTMENT OFFERING MEMORANDUM", "Confidential"),
        filler("INVESTMENT HIGHLIGHTS", "16 units", "Asking price $1,050,000"),
        filler("PROPERTY PHOTOS", "Front", "Rear"),
        filler("STABILIZED UPSIDE PRO FORMA", "Unit 207 is in the current rent roll at $775"),
        rent_roll_page(),
        t12_page(),
        filler("DISCLAIMER", "This document is confidential."),
    ]
    return pages_pdf(path, pages)


def test_a_brochure_is_routed_to_its_rent_roll_and_t12_pages(tmp_path: Path) -> None:
    routing = route_pdf(brochure(tmp_path / "package.pdf"), CFG.rr_headers)
    assert [r.page for r in routing.rent_roll or []] == [5]
    assert [r.page for r in routing.t12 or []] == [6]
    assert "7-page file" in routing.notes[-1]


def test_a_pro_forma_that_mentions_the_rent_roll_is_not_taken_for_it(tmp_path: Path) -> None:
    routing = route_pdf(brochure(tmp_path / "package.pdf"), CFG.rr_headers)
    assert 4 not in [r.page for r in routing.rent_roll or []]


def test_a_brochure_runs_end_to_end_from_one_file(tmp_path: Path) -> None:
    deal = tmp_path / "om_deal"
    deal.mkdir()
    brochure(deal / "rent_roll.pdf")
    pkg = run_deal(deal, llm=CannedCoaLLM(), cfg=CFG, as_of=AS_OF, out_dir=tmp_path / "out")
    assert len(pkg.rent_roll.units) == 8
    assert {u.citations["unit_id"].page for u in pkg.rent_roll.units} == {5}
    assert pkg.t12 is not None and {ln.citation.page for ln in pkg.t12.lines} == {6}


def test_a_brochure_in_both_slots_reads_each_table_from_its_own_pages(tmp_path: Path) -> None:
    deal = tmp_path / "om_twice"
    deal.mkdir()
    for name in ("rent_roll.pdf", "t12.pdf"):
        brochure(deal / name)
    pkg = run_deal(deal, llm=CannedCoaLLM(), cfg=CFG, as_of=AS_OF, out_dir=tmp_path / "out")
    assert len(pkg.rent_roll.units) == 8 and pkg.t12 is not None and len(pkg.t12.lines) == 8


def test_a_long_rent_roll_that_is_not_a_brochure_is_read_whole(tmp_path: Path) -> None:
    pages = [rent_roll_page(units=8), rent_roll_page(units=8)]
    routing = route_pdf(pages_pdf(tmp_path / "rr.pdf", pages), CFG.rr_headers)
    assert routing.rent_roll is None and routing.t12 is None


def test_a_brochure_with_no_rent_roll_has_nothing_to_route(tmp_path: Path) -> None:
    pdf = pages_pdf(
        tmp_path / "om.pdf",
        [filler("PROPERTY PHOTOS", "x"), filler("PRO FORMA", "y")] * 3 + [t12_page()],
    )
    routing = route_pdf(pdf, CFG.rr_headers)
    assert not routing.has_rent_roll and routing.has_t12


# ---------- (e) digital page that the text reader cannot read -> vision ----------


class ScriptedVisionLLM:
    """Answers vision calls with a fixed page; header and COA calls go to the canned client."""

    def __init__(self, page: VisionTablePage) -> None:
        self.page = page
        self.tasks: list[str] = []
        self.pages_seen = 0
        self._canned = CannedCoaLLM()

    def structured(self, *, task: str, response_model: Any, system: str, user_content, model=None):
        self.tasks.append(task)
        if task == "vision_page":
            self.pages_seen += 1
            return self.page
        return self._canned.structured(
            task=task,
            response_model=response_model,
            system=system,
            user_content=user_content,
            model=model,
        )


def rr_vision_page() -> VisionTablePage:
    headers = ["Unit", "Tenant", "Status", "Current Rent"]
    rows = [
        VisionRow(cells={h: h for h in headers}, row_kind="header"),
        VisionRow(
            cells={"Unit": "101", "Tenant": "A", "Status": "Active", "Current Rent": "$650"},
            row_kind="unit",
        ),
        VisionRow(
            cells={"Unit": "102", "Tenant": "", "Status": "Vacant", "Current Rent": ""},
            row_kind="unit",
        ),
        VisionRow(cells={"Unit": "RENT ROLL AS OF APRIL 3, 2026"}, row_kind="other"),
    ]
    return VisionTablePage(headers=headers, rows=rows)


def unreadable_rent_roll(path: Path) -> Path:
    """Digital text, but no column structure the table reader can use."""
    return text_pdf(path, [["Rent roll", "the units are listed in a paragraph: 101 650 102 700"]])


def test_a_digital_pdf_the_text_reader_cannot_read_falls_back_to_the_page_image(
    tmp_path: Path,
) -> None:
    pdf = unreadable_rent_roll(tmp_path / "rent_roll.pdf")
    llm = ScriptedVisionLLM(rr_vision_page())
    rent_roll, read = read_rent_roll(
        pdf,
        CFG,
        sheet=None,
        cache_dir=tmp_path / "c",
        llm=llm,
        routing=route_pdf(pdf, CFG.rr_headers),
    )
    assert llm.pages_seen == 1
    assert [u.unit_id for u in rent_roll.units] == ["101", "102"]
    assert rent_roll.units[0].citations["unit_id"].provenance == "llm_vision"
    assert any("text layer reader could not read" in n for n in read.notes)
    assert any("wrapped cells rejoined reader could not read" in n for n in read.notes)
    assert rent_roll.extraction_warnings[0].startswith("The text layer reader")


def test_a_file_the_text_reader_can_read_never_reaches_the_vision_step(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "rent_roll.pdf", [rent_roll_page()])
    llm = ScriptedVisionLLM(rr_vision_page())
    rent_roll, read = read_rent_roll(
        pdf,
        CFG,
        sheet=None,
        cache_dir=tmp_path / "c",
        llm=llm,
        routing=route_pdf(pdf, CFG.rr_headers),
    )
    assert llm.pages_seen == 0 and len(rent_roll.units) == 8 and read.notes == []


def test_wrapped_cells_are_healed_before_any_page_image_is_paid_for(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "rent_roll.pdf", [rent_roll_page(wrapped=True)])
    llm = ScriptedVisionLLM(rr_vision_page())
    rent_roll, read = read_rent_roll(
        pdf,
        CFG,
        sheet=None,
        cache_dir=tmp_path / "c",
        llm=llm,
        routing=route_pdf(pdf, CFG.rr_headers),
    )
    assert llm.pages_seen == 0 and len(rent_roll.units) == 8
    assert len(read.notes) == 1 and "text layer reader could not read" in read.notes[0]


def test_vision_is_not_used_on_a_long_file_without_routed_pages(tmp_path: Path) -> None:
    pdf = pages_pdf(tmp_path / "om.pdf", [filler("PROPERTY PHOTOS", "x")] * 6)
    llm = ScriptedVisionLLM(rr_vision_page())
    with pytest.raises(Exception, match="unit rows"):
        read_rent_roll(
            pdf,
            CFG,
            sheet=None,
            cache_dir=tmp_path / "c",
            llm=llm,
            routing=route_pdf(pdf, CFG.rr_headers),
        )
    assert llm.pages_seen == 0, "six pages of images are not sent without a routed page"


def test_the_vision_reader_only_sees_the_routed_pages(tmp_path: Path) -> None:
    bad_roll = [(50, 100, "RENT ROLL"), (50, 120, "listed in a paragraph: 101 650")]
    pages = [
        filler("PROPERTY PHOTOS", "x"),
        filler("INVESTMENT HIGHLIGHTS", "y"),
        filler("PRO FORMA", "z"),
        bad_roll,
        t12_page(),
    ]
    pdf = pages_pdf(tmp_path / "om.pdf", pages)
    routing = route_pdf(pdf, CFG.rr_headers)
    assert not routing.has_rent_roll  # nothing routable: vision must not be tried on 5 pages
    llm = ScriptedVisionLLM(rr_vision_page())
    with pytest.raises(Exception):  # noqa: B017
        read_rent_roll(pdf, CFG, sheet=None, cache_dir=tmp_path / "c", llm=llm, routing=routing)
    assert llm.pages_seen == 0


def test_a_failed_cascade_raises_the_last_readers_error(tmp_path: Path) -> None:
    pdf = unreadable_rent_roll(tmp_path / "rent_roll.pdf")

    class Refuses:
        def structured(self, **kwargs):
            raise HeaderMapError("the page could not be read")

    with pytest.raises(HeaderMapError):
        read_rent_roll(pdf, CFG, sheet=None, cache_dir=tmp_path / "c", llm=Refuses(), routing=None)


def t12_vision_page() -> VisionTablePage:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    headers = ["Line Item", *months, "Total"]
    rows = [
        VisionRow(cells={"Line Item": "T-12 JANUARY THROUGH DECEMBER 2025"}, row_kind="other"),
        VisionRow(cells={h: h for h in headers}, row_kind="header"),
        VisionRow(
            cells={"Line Item": "Utilities", **{m: "100" for m in months}, "Total": "1,200"},
            row_kind="unit",
        ),
        VisionRow(
            cells={"Line Item": "Landscaping", **{m: "50" for m in months}, "Total": "600"},
            row_kind="unit",
        ),
        VisionRow(
            cells={"Line Item": "Total Expenses", **{m: "150" for m in months}, "Total": "1,800"},
            row_kind="subtotal",
        ),
    ]
    return VisionTablePage(headers=headers, rows=rows)


class ScriptedT12LLM(ScriptedVisionLLM):
    def structured(self, *, task: str, response_model: Any, system: str, user_content, model=None):
        if task == "vision_page":
            self.tasks.append(task)
            self.pages_seen += 1
            prompt = user_content[-1]["text"]
            assert "T-12" in prompt, "a T-12 gets the T-12 prompt"
            return self.page
        return super().structured(
            task=task,
            response_model=response_model,
            system=system,
            user_content=user_content,
            model=model,
        )


def test_an_unreadable_digital_t12_falls_back_to_the_page_image(tmp_path: Path) -> None:
    pdf = text_pdf(tmp_path / "t12.pdf", [["Trailing twelve", "income and expenses in prose"]])
    llm = ScriptedT12LLM(t12_vision_page())
    statement, read = read_t12(
        pdf,
        CFG,
        cache_dir=tmp_path / "c",
        llm=llm,
        learned_path=tmp_path / "learned.yaml",
        routing=route_pdf(pdf, CFG.rr_headers),
    )
    assert llm.pages_seen == 1
    assert [ln.label for ln in statement.lines] == ["Utilities", "Landscaping"]
    assert statement.period_end == date(2025, 12, 31)
    assert statement.source_format == "pdf_scanned"
    assert statement.lines[0].monthly[0] == D("100.00")
    assert len(read.notes) == 2


def test_vision_fallback_calls_are_recorded_in_the_cost_ledger(tmp_path: Path) -> None:
    from cre_screen.mapping.cache import LLMCache
    from cre_screen.mapping.cost import CostTracker
    from cre_screen.mapping.llm_client import AnthropicLLMClient

    class Instructor:
        def __init__(self) -> None:
            self.calls = 0
            self.handlers: dict[str, Any] = {}
            from types import SimpleNamespace

            self.messages = SimpleNamespace(create=self.create)

        def on(self, name: str, handler: Any) -> None:
            self.handlers[name] = handler

        def create(self, **kwargs: Any):
            from types import SimpleNamespace

            self.calls += 1
            usage = SimpleNamespace(input_tokens=1800, output_tokens=400)
            self.handlers["completion:response"](SimpleNamespace(usage=usage))
            return rr_vision_page()

    models = CFG.models
    cache = LLMCache(tmp_path / "cache", mode="record", version=models.cache.version)
    tracker = CostTracker(models, tmp_path / "ledger.jsonl", run_id="r")
    fake = Instructor()
    client = AnthropicLLMClient(models, cache, tracker, instructor_client=fake)
    pdf = unreadable_rent_roll(tmp_path / "rent_roll.pdf")
    kwargs = dict(
        sheet=None, cache_dir=tmp_path / "c", llm=client, routing=route_pdf(pdf, CFG.rr_headers)
    )
    first, _ = read_rent_roll(pdf, CFG, **kwargs)  # type: ignore[arg-type]
    assert fake.calls >= 1 and tracker.run_total() > 0
    lines = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert {row["task"] for row in lines} >= {"vision_page"}
    before = fake.calls
    second, _ = read_rent_roll(pdf, CFG, **kwargs)  # type: ignore[arg-type]
    assert fake.calls == before, "the second read is served from the cache"
    assert [u.unit_id for u in first.units] == [u.unit_id for u in second.units]
