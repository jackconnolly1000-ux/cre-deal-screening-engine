"""Pre-flight diagnostics: plain-English problems found before the pipeline runs."""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from cre_screen.ingest.preflight import diagnose_pdf, preflight
from tests.pdf_makers import (
    both_on_one_page,
    filler,
    pages_pdf,
    rent_roll_page,
    t12_page,
)

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """One PDF with a text layer: each page is a list of lines."""
    pdf = canvas.Canvas(str(path), pagesize=letter, invariant=1)
    for lines in pages:
        y = 740
        for line in lines:
            pdf.drawString(50, y, line)
            y -= 16
        pdf.showPage()
    pdf.save()
    return path


def damaged_pdf(path: Path) -> Path:
    """A one-page PDF whose only content is an image with a broken Flate stream."""
    garbage = b"this is not zlib data at all" * 8
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /XObject << /Im0 5 0 R >> >> >>",
        b"<< /Length 36 >>\nstream\nq 200 0 0 200 0 0 cm /Im0 Do Q\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 40 /Height 40 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length "
        + str(len(garbage)).encode()
        + b" >>\nstream\n"
        + garbage
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(bytes(out))
    return path


ROLL_PAGE = ["RENT ROLL AS OF 4/3/2026", "Unit Tenant Status Rent"] + [
    f"{100 + n} Tenant {n} Active $650" for n in range(1, 9)
]
T12_PAGE = ["T-12 FINANCIAL STATEMENT", "Jan Feb Mar", "Total Income 100 200 300", "Expenses 10 20"]


def codes(found) -> list[str]:
    return [d.code for d in found]


# ---------- files that should pass ----------


@pytest.mark.parametrize("deal", ["deal_001", "deal_003", "deal_010", "deal_009", "deal_012"])
def test_the_dev_deals_raise_no_diagnostics(deal: str) -> None:
    folder = DATA / deal
    rr = next(p for p in (folder / "rent_roll.pdf", folder / "rent_roll.xlsx") if p.exists())
    t12 = next(p for p in (folder / "t12.pdf", folder / "t12.xlsx") if p.exists())
    assert preflight(rr, t12) == []


def test_a_clean_rent_roll_and_t12_pass(tmp_path: Path) -> None:
    rr = pages_pdf(tmp_path / "rent_roll.pdf", [rent_roll_page()])
    t12 = pages_pdf(tmp_path / "t12.pdf", [t12_page()])
    assert preflight(rr, t12) == []


# ---------- damaged and blank files ----------


def test_a_pdf_with_a_broken_image_stream_is_reported_as_damaged(tmp_path: Path) -> None:
    (found,) = diagnose_pdf(damaged_pdf(tmp_path / "rent_roll.pdf"), "rent_roll")
    assert found.code == "damaged_pdf" and found.severity == "error"
    assert "Download the document again" in found.steps[0]
    assert found.file == "rent_roll.pdf"


def test_a_file_that_is_not_a_pdf_is_reported_as_damaged(tmp_path: Path) -> None:
    bad = tmp_path / "t12.pdf"
    bad.write_bytes(b"not a pdf")
    assert codes(diagnose_pdf(bad, "t12")) == ["damaged_pdf"]


def test_a_blank_page_is_reported(tmp_path: Path) -> None:
    pdf = canvas.Canvas(str(tmp_path / "rent_roll.pdf"), pagesize=letter)
    pdf.showPage()
    pdf.save()
    found = diagnose_pdf(tmp_path / "rent_roll.pdf", "rent_roll")
    assert codes(found) == ["blank_pages"] and "Page(s) 1" in found[0].detail


# ---------- brochures and wrong documents ----------


def om_pages() -> list:
    return [
        filler("INVESTMENT OFFERING MEMORANDUM"),
        filler("INVESTMENT HIGHLIGHTS"),
        filler("PROPERTY PHOTOS"),
        filler("PRO FORMA INCOME STATEMENT"),
        rent_roll_page(),
        t12_page(),
    ]


def test_a_whole_brochure_is_accepted_and_the_routed_pages_are_reported(tmp_path: Path) -> None:
    brochure = pages_pdf(tmp_path / "package.pdf", om_pages())
    (note,) = diagnose_pdf(brochure, "rent_roll")
    assert note.code == "auto_routed" and note.severity == "info" and not note.steps
    assert "rent roll on page(s) 5" in note.detail and "T-12 on page(s) 6" in note.detail
    assert preflight(brochure, None) == [note]


def test_a_brochure_in_both_slots_raises_no_error(tmp_path: Path) -> None:
    brochure = pages_pdf(tmp_path / "package.pdf", om_pages())
    found = preflight(brochure, brochure)
    assert found and all(d.severity == "info" for d in found)


def test_a_brochure_without_a_rent_roll_is_blocked(tmp_path: Path) -> None:
    pages = [filler("PROPERTY PHOTOS"), filler("PRO FORMA"), t12_page()] * 2
    (found,) = diagnose_pdf(pages_pdf(tmp_path / "package.pdf", pages), "rent_roll")
    assert found.code == "no_unit_rows" and "any of its 6 pages" in found.detail


def test_a_page_with_both_tables_is_accepted_in_either_slot(tmp_path: Path) -> None:
    flyer = pages_pdf(tmp_path / "flyer.pdf", [both_on_one_page()])
    assert preflight(flyer, flyer) == preflight(flyer, None) or all(
        d.severity == "info" for d in preflight(flyer, flyer)
    )
    assert not [d for d in preflight(flyer, flyer) if d.severity == "error"]


def test_a_profit_and_loss_in_the_rent_roll_slot_is_reported(tmp_path: Path) -> None:
    pnl = make_pdf(
        tmp_path / "rent_roll.pdf",
        [
            [
                "Profit & Loss Statement 2024",
                "Rental Income 800 800",
                "Total Income 1600",
                "Expenses 5",
            ]
        ],
    )
    (found,) = diagnose_pdf(pnl, "rent_roll")
    assert found.code == "not_a_rent_roll" and "profit and loss" in found.title


def test_a_summary_sheet_without_unit_rows_is_reported(tmp_path: Path) -> None:
    summary = make_pdf(
        tmp_path / "rent_roll.pdf",
        [["UNIT MIX SUMMARY", "Type Count Avg Rent", "1BR 8 $650", "2BR 8 $760", "Total 16 $705"]],
    )
    (found,) = diagnose_pdf(summary, "rent_roll")
    assert found.code == "no_unit_rows" and "unit-by-unit" in found.steps[0]


def test_a_rent_roll_in_the_t12_slot_is_reported(tmp_path: Path) -> None:
    roll = make_pdf(
        tmp_path / "t12.pdf", [["RENT ROLL", "Tenant Lease Start Lease End", *ROLL_PAGE]]
    )
    assert codes(diagnose_pdf(roll, "t12")) == ["not_a_t12"]


def test_the_same_file_in_both_slots_is_not_blocked_when_both_tables_exist(tmp_path: Path) -> None:
    flyer = pages_pdf(tmp_path / "flyer.pdf", [both_on_one_page()])
    copy = tmp_path / "t12.pdf"
    copy.write_bytes(flyer.read_bytes())
    assert not [d for d in preflight(flyer, copy) if d.severity == "error"]


def test_a_profit_and_loss_in_both_slots_blocks_only_the_missing_rent_roll(tmp_path: Path) -> None:
    pnl = pages_pdf(tmp_path / "t12.pdf", [t12_page(heading="Profit & Loss Statement 2024")])
    copy = tmp_path / "rent_roll.pdf"
    copy.write_bytes(pnl.read_bytes())
    found = preflight(copy, pnl)
    assert [(d.code, d.file) for d in found] == [("not_a_rent_roll", "rent_roll.pdf")]


def test_mirrored_text_is_reported(tmp_path: Path) -> None:
    lines = ["emocnI latoT", "sesnepxE latoT", "tnemetatS emocnI", "latoT"]
    mirrored = make_pdf(tmp_path / "t12.pdf", [lines])
    assert codes(diagnose_pdf(mirrored, "t12")) == ["mirrored_text"]


def test_excel_files_and_a_missing_t12_pass_through(tmp_path: Path) -> None:
    xlsx = tmp_path / "rent_roll.xlsx"
    xlsx.write_bytes(b"PK")
    assert preflight(xlsx, None) == []
