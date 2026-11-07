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

from cre_screen.config import RRHeadersConfig, load_config
from cre_screen.ingest.render import PdfHealth, pdf_health
from cre_screen.ingest.routing import Routing, route_pdf

Severity = Literal["error", "warning", "info"]

PNL_WORDS = ("profit & loss", "profit and loss", "income statement", "total income", "expenses")
PNL_TITLES = ("profit & loss", "profit and loss", "income statement")
ROLL_WORDS = ("rent roll", "tenant", "lease start", "lease end", "move-in")
FORWARD = ("income", "total", "expenses", "rent", "unit", "tenant", "lease")
REVERSED = tuple(word[::-1] for word in FORWARD)


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


def _rent_roll_content(name: str, text: str, routing: Routing) -> list[Diagnostic]:
    if not text.strip() or routing.has_rent_roll:
        return []  # scanned (the vision step reads it) or a unit-level table was found
    titled = any(w in text[:300] for w in PNL_TITLES)
    is_pnl = titled or (
        sum(w in text for w in PNL_WORDS) >= 2 and not any(w in text for w in ROLL_WORDS[:3])
    )
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
    where = f"any of its {routing.pages} pages" if routing.pages > 1 else "it"
    return [
        Diagnostic(
            "no_unit_rows",
            "error",
            name,
            "No unit-level rent roll was found",
            f"No table with one row per unit (unit number, rent, status) appears on {where}. "
            "Summary sheets such as a unit mix or an income recap total the rents without "
            "listing each unit, so the rent roll cannot be checked against them.",
            (
                "Ask the seller or broker for the unit-by-unit rent roll.",
                "Upload that file; a unit mix table or summary page cannot be screened.",
            ),
        )
    ]


def _t12_content(name: str, text: str, routing: Routing) -> list[Diagnostic]:
    if not text.strip() or routing.has_t12:
        return []
    if sum(w in text for w in ROLL_WORDS) >= 3 and "total income" not in text:
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
    if not any(w in text for w in ("income", "expenses", "revenue")):
        return [
            Diagnostic(
                "no_t12_table",
                "error",
                name,
                "No T-12 was found",
                "The file has no income and expense lines by month.",
                ("Upload the trailing-12 operating statement (income and expenses by month).",),
            )
        ]
    return []  # it talks about income and expenses; the readers get a chance at it


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _routed(name: str, routing: Routing) -> list[Diagnostic]:
    if not routing.notes:
        return []
    return [
        Diagnostic(
            "auto_routed",
            "info",
            name,
            "The financial pages were picked out of this file",
            _sentence("; ".join(routing.notes)) + ". Only those pages are read.",
            (),
        )
    ]


def diagnose_pdf(
    path: Path, kind: Literal["rent_roll", "t12"], cfg: RRHeadersConfig | None = None
) -> list[Diagnostic]:
    """Diagnostics for one PDF. An empty list means nothing was found."""
    name = path.name
    health = pdf_health(path)
    found = _damaged(name, health)
    if found:
        return found  # nothing else can be trusted from a damaged file
    found += _blank(name, health)
    text = _flat(health.texts)
    found += _mirrored(name, text)
    if found or not text.strip():
        return found
    routing = route_pdf(path, cfg or load_config().rr_headers)
    if kind == "rent_roll":
        found += _rent_roll_content(name, text, routing)
    else:
        found += _t12_content(name, text, routing)
    return found or _routed(name, routing)


def preflight(
    rent_roll: Path | None, t12: Path | None, cfg: RRHeadersConfig | None = None
) -> list[Diagnostic]:
    """Diagnostics for the files of one deal. Excel files and scans pass through.

    A file only has to hold the tables it is there for: one file may serve both slots, and one
    page may hold both tables. A diagnostic is an error only when a required table is truly absent.
    """
    found: list[Diagnostic] = []
    for path, kind in ((rent_roll, "rent_roll"), (t12, "t12")):
        if path is not None and path.suffix.lower() == ".pdf":
            found += diagnose_pdf(path, kind, cfg)  # type: ignore[arg-type]
    return found
