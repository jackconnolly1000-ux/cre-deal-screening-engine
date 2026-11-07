"""Pre-flight checks on the files a reviewer is about to screen.

The pipeline fails with a parser message ("no header row found") when a file is damaged, is a whole
offering memorandum, or is not a unit-level rent roll. Those messages say what the parser saw, not
what the person should do. This module looks at the files first and returns plain-English
diagnostics with remediation steps. It only reads; it never changes a file and never calls the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cre_screen.ingest.render import PdfHealth, pdf_health

Severity = Literal["error", "warning"]

SECTION_WORDS = {
    "rent roll": ("rent roll",),
    "T-12": ("t-12", "t12", "trailing 12", "trailing twelve"),
    "pro forma": ("pro forma", "pro-forma"),
    "photos": ("property photos",),
    "highlights": ("investment highlights", "offering memorandum"),
}
PNL_WORDS = ("profit & loss", "profit and loss", "income statement", "total income", "expenses")
PNL_TITLES = ("profit & loss", "profit and loss", "income statement")
ROLL_WORDS = ("rent roll", "tenant", "lease start", "lease end", "move-in")
FORWARD = ("income", "total", "expenses", "rent", "unit", "tenant", "lease")
REVERSED = tuple(word[::-1] for word in FORWARD)
UNIT_ROW = re.compile(r"(?m)^\s*[A-Za-z]{0,2}\d{1,4}[A-Za-z]?(?:\s|$)")
MIN_UNIT_ROWS = 3
PACKAGE_PAGES = 4  # a rent roll or T-12 file longer than this is probably a whole package


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    file: str
    title: str
    detail: str
    steps: tuple[str, ...] = field(default_factory=tuple)


def _flat(texts: list[str]) -> str:
    return " ".join(" ".join(texts).split()).lower()


def _damaged(name: str, health: PdfHealth) -> list[Diagnostic]:
    problems = [
        w
        for w in health.warnings
        if re.search(r"zlib|corrupt|invalid|incorrect header|cannot (open|read)", w, re.I)
        or ("error" in w.lower() and "xref" not in w.lower())
    ]
    if health.open_error or problems:
        reason = health.open_error or problems[0]
        return [
            Diagnostic(
                "damaged_pdf",
                "error",
                name,
                "This PDF is damaged",
                f"The PDF library reported: {reason}. Pages built from damaged image data open "
                "blank, so nothing can be read from them.",
                (
                    "Download the document again from the original source.",
                    "Open it in a PDF viewer. If the pages show, use Print > Save as PDF to "
                    "write a clean copy, and upload that copy.",
                    "If the pages show blank in the viewer too, ask the sender for a new export.",
                ),
            )
        ]
    return []


def _blank(name: str, health: PdfHealth) -> list[Diagnostic]:
    if not health.blank_pages:
        return []
    pages = ", ".join(str(p) for p in health.blank_pages)
    return [
        Diagnostic(
            "blank_pages",
            "error",
            name,
            "Some pages are blank",
            f"Page(s) {pages} have no text and render as an empty page, so they hold no rent "
            "roll or T-12 data that could be read.",
            (
                "Check the original: a blank page usually means a failed export or scan.",
                "Re-export or re-scan those pages, then upload the new file.",
            ),
        )
    ]


def _mirrored(name: str, text: str) -> list[Diagnostic]:
    forward = sum(text.count(w) for w in FORWARD)
    backward = sum(text.count(w) for w in REVERSED)
    if backward > forward and backward >= 3:
        return [
            Diagnostic(
                "mirrored_text",
                "error",
                name,
                "The text is upside down or mirrored",
                "The words are stored back to front (for example 'emocnI' for 'Income'). The "
                "parser reads them in file order, so no table can be found.",
                (
                    "Open the file in a PDF viewer and rotate the page upright.",
                    "Use Print > Save as PDF so the text is stored the right way round.",
                    "Upload the corrected copy.",
                ),
            )
        ]
    return []


def _package(name: str, health: PdfHealth) -> list[Diagnostic]:
    found: dict[str, list[int]] = {}
    for number, text in enumerate(health.texts, start=1):
        lowered = " ".join(text.split()).lower()
        for kind, words in SECTION_WORDS.items():
            if any(word in lowered for word in words):
                found.setdefault(kind, []).append(number)
    kinds = {k: v for k, v in found.items() if k != "highlights"}
    if health.pages > PACKAGE_PAGES and (len(kinds) >= 2 or "highlights" in found):
        where = "; ".join(f"{k}: page {', '.join(map(str, v))}" for k, v in kinds.items())
        return [
            Diagnostic(
                "multi_page_package",
                "error",
                name,
                "This looks like a whole brochure, not a single document",
                f"The file has {health.pages} pages with several kinds of content"
                f"{f' ({where})' if where else ''}. The screen reads one rent roll and one T-12.",
                (
                    "Copy the rent roll pages into their own file named rent_roll.pdf.",
                    "Copy the T-12 pages into their own file named t12.pdf.",
                    "Upload those two files instead of the brochure.",
                ),
            )
        ]
    return []


def _rent_roll_content(name: str, text: str, health: PdfHealth) -> list[Diagnostic]:
    if not text.strip():
        return []  # scanned: the vision step reads it
    titled = any(w in text[:300] for w in PNL_TITLES)
    is_pnl = titled or (
        sum(w in text for w in PNL_WORDS) >= 2 and not any(w in text for w in ROLL_WORDS[:3])
    )
    rows = sum(len(UNIT_ROW.findall(t)) for t in health.texts)
    if is_pnl:
        return [
            Diagnostic(
                "not_a_rent_roll",
                "error",
                name,
                "This file is a profit and loss statement, not a rent roll",
                "It has income and expense lines by month but no unit rows or tenant columns.",
                (
                    "Find the unit-by-unit rent roll in the package (one row per unit).",
                    "Upload it as the rent roll. The profit and loss belongs in the T-12 slot.",
                ),
            )
        ]
    if rows < MIN_UNIT_ROWS:
        return [
            Diagnostic(
                "no_unit_rows",
                "error",
                name,
                "No unit-level rows were found",
                f"Only {rows} line(s) start like a unit number. Summary sheets such as a unit "
                "mix or an income recap total the rents without listing each unit, so the "
                "rent roll cannot be checked against them.",
                (
                    "Ask the seller or broker for the unit-by-unit rent roll.",
                    "Upload that file; a unit mix table or summary page cannot be screened.",
                ),
            )
        ]
    return []


def _t12_content(name: str, text: str) -> list[Diagnostic]:
    if text.strip() and sum(w in text for w in ROLL_WORDS) >= 3 and "total income" not in text:
        return [
            Diagnostic(
                "not_a_t12",
                "error",
                name,
                "This file looks like a rent roll, not a T-12",
                "It lists tenants and leases but has no monthly income and expense lines.",
                ("Upload the trailing-12 operating statement (income and expenses by month).",),
            )
        ]
    return []


def diagnose_pdf(path: Path, kind: Literal["rent_roll", "t12"]) -> list[Diagnostic]:
    """Diagnostics for one PDF. An empty list means nothing was found."""
    name = path.name
    health = pdf_health(path)
    found = _damaged(name, health)
    if found:
        return found  # nothing else can be trusted from a damaged file
    found += _blank(name, health)
    text = _flat(health.texts)
    found += _mirrored(name, text)
    if found:
        return found
    found += _package(name, health)
    if found:
        return found
    found += _rent_roll_content(name, text, health) if kind == "rent_roll" else []
    found += _t12_content(name, text) if kind == "t12" else []
    return found


def _text_of(path: Path) -> str:
    return _flat(pdf_health(path).texts)


def preflight(rent_roll: Path | None, t12: Path | None) -> list[Diagnostic]:
    """Diagnostics for the files of one deal. Excel files and scans pass through."""
    found: list[Diagnostic] = []
    for path, kind in ((rent_roll, "rent_roll"), (t12, "t12")):
        if path is not None and path.suffix.lower() == ".pdf":
            found += diagnose_pdf(path, kind)  # type: ignore[arg-type]
    if (
        rent_roll is not None
        and t12 is not None
        and rent_roll.suffix.lower() == t12.suffix.lower() == ".pdf"
    ):
        a, b = _text_of(rent_roll), _text_of(t12)
        if a and a == b:
            found.append(
                Diagnostic(
                    "same_document",
                    "error",
                    f"{rent_roll.name} and {t12.name}",
                    "The rent roll and the T-12 are the same document",
                    "Both files contain exactly the same text, so one of them is the wrong file.",
                    (
                        "Check which document is missing from the package.",
                        "Upload the real rent roll and T-12 as two different files.",
                    ),
                )
            )
    return found
