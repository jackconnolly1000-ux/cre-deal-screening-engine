"""An offline stand-in for the vision model: flawless OCR of the synthetic scanned deals.

The real vision path runs unchanged (embedded image, base64, one call per page). The stand-in
recognizes each page by the SHA-256 of its image bytes and answers with a perfect transcription of
the clean (pre-degradation) layout, taken from that layout's own text layer. Nothing is called or
spent, and it is independent of which OS or library version decoded the JPEG.
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
from pathlib import Path
from typing import Any

from cre_screen.config import load_config
from cre_screen.ingest.pdf_tables import extract_pdf_table
from cre_screen.ingest.raw import RawRow, RawTable
from cre_screen.ingest.render import page_count, page_image
from cre_screen.schemas.mapping import VisionRow, VisionTablePage
from synth.layouts import RR
from synth.params import load_params
from synth.plan import PLAN
from synth.truth import build_deal

KIND = {"unit": "unit", "subtotal": "subtotal", "header": "header", "footnote": "footnote"}


def _page_from_table(table: RawTable, page: int) -> VisionTablePage:
    headers = list(table.headers)
    rows: list[VisionRow] = []
    if page == 1:
        for line in table.preamble_text.splitlines():
            rows.append(VisionRow(cells={headers[0]: line}, row_kind="other"))
    for raw in table.rows:
        if raw.page != page or raw.kind == "blank":
            continue
        rows.append(_vision_row(raw, headers))
    return VisionTablePage(headers=headers, rows=rows)


def _vision_row(raw: RawRow, headers: list[str]) -> VisionRow:
    cells = {h: raw.cells[h].text if h in raw.cells else "" for h in headers}
    if raw.kind == "header":
        cells = {h: h for h in headers}
    return VisionRow(cells=cells, row_kind=KIND.get(raw.kind, "other"))  # type: ignore[arg-type]


def clean_transcriptions(
    deal_number: int, scanned_pdf: Path, seed: int = 7
) -> dict[str, VisionTablePage]:
    """{sha256 of the image the model will be shown: perfect transcription of that page}."""
    build = build_deal(deal_number, seed, load_params())
    plan = PLAN[deal_number]
    cfg = load_config()
    with tempfile.TemporaryDirectory() as scratch:
        clean = Path(scratch) / "rent_roll.pdf"
        RR[plan.rr_layout].render(
            clean,
            units=build.truth.units,
            stated_totals=build.truth.rr_stated_totals,
            as_of=build.truth.as_of_date,
            style=build.style,
            footnotes=build.footnotes,
        )
        table = extract_pdf_table(clean, cfg.rr_headers)
    out: dict[str, VisionTablePage] = {}
    for page in range(1, page_count(scanned_pdf) + 1):
        data, _media = page_image(scanned_pdf, page)
        out[hashlib.sha256(base64.b64encode(data)).hexdigest()] = _page_from_table(table, page)
    return out


class PerfectOcr:
    """Handles ``vision_page`` calls; everything else goes to ``fallback``."""

    def __init__(self, pages: dict[str, VisionTablePage], fallback: Any = None) -> None:
        self.pages = pages
        self.fallback = fallback
        self.vision_calls: list[dict[str, Any]] = []

    def structured(self, *, task: str, response_model: Any, system: str, user_content, model=None):
        if task != "vision_page":
            if self.fallback is None:
                raise AssertionError(f"unexpected LLM task {task}")
            return self.fallback.structured(
                task=task,
                response_model=response_model,
                system=system,
                user_content=user_content,
                model=model,
            )
        image = next(b for b in user_content if b.get("type") == "image")
        key = hashlib.sha256(image["source"]["data"].encode("ascii")).hexdigest()
        self.vision_calls.append({"system": system, "media_type": image["source"]["media_type"]})
        if key not in self.pages:
            raise AssertionError("PerfectOcr was shown a page it has no transcription for")
        return self.pages[key]


def perfect_ocr_for(deals: dict[int, Path], fallback: Any = None, seed: int = 7) -> PerfectOcr:
    """``deals`` maps deal number to the folder holding its scanned rent_roll.pdf."""
    pages: dict[str, VisionTablePage] = {}
    for number, folder in deals.items():
        pages.update(clean_transcriptions(number, folder / "rent_roll.pdf", seed))
    return PerfectOcr(pages, fallback)
