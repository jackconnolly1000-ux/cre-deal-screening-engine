"""Vision fallback for scanned rent rolls (SPEC 7.6).

The model transcribes each page as *strings*; code parses them downstream. Every value that
reaches a typed record this way carries ``provenance="llm_vision"``. Each page is its own call, so
a prompt stays far below ``max_prompt_tokens``. The result is a ``RawTable`` with no boxes, only
page and row index, and then flows through header mapping and rent roll building unchanged.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from cre_screen.errors import CreScreenError
from cre_screen.ingest.raw import RawCell, RawRow, RawTable, RowKind
from cre_screen.ingest.render import page_count, page_image
from cre_screen.mapping.header_mapper import normalize_header
from cre_screen.mapping.llm_protocol import LLMClient
from cre_screen.mapping.prompts import SYSTEM_LINE, render_prompt
from cre_screen.schemas.mapping import VisionTablePage

logger = logging.getLogger(__name__)

RENDER_DPI = 110
CAREFUL_SUFFIX = (
    " This is a second, independent reading of the same page. Read slowly and check every digit "
    "of every amount and date before answering."
)
ROW_KINDS: dict[str, RowKind] = {
    "unit": "unit",
    "subtotal": "subtotal",
    "header": "header",
    "footnote": "footnote",
    "other": "footnote",
}


class VisionError(CreScreenError):
    """The scanned page could not be turned into a table."""


def transcribe_page(
    pdf: Path, page: int, llm: LLMClient, *, careful: bool = False
) -> VisionTablePage:
    """One vision call. ``careful=True`` changes the system prompt, so it caches separately."""
    data, media_type = page_image(pdf, page, dpi=RENDER_DPI)
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        },
        {"type": "text", "text": render_prompt("vision_page")},
    ]
    return llm.structured(
        task="vision_page",
        response_model=VisionTablePage,
        system=SYSTEM_LINE + (CAREFUL_SUFFIX if careful else ""),
        user_content=content,
    )


def _unique(texts: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for text in texts:
        seen[text] = seen.get(text, 0) + 1
        out.append(text if seen[text] == 1 else f"{text} ({seen[text]})")
    return out


def canonical_key(
    key: str, page_headers: list[str], canonical: list[str], by_norm: dict[str, str]
) -> str | None:
    """Map a cell key to a page-1 header: by column position if the page agrees, else by text."""
    if key in page_headers and len(page_headers) == len(canonical):
        return canonical[page_headers.index(key)]
    return by_norm.get(normalize_header(key))


def page_rows(
    vp: VisionTablePage, canonical: list[str]
) -> list[tuple[RowKind, dict[str, str], dict[str, str]]]:
    """(kind, cells keyed by canonical header, all raw cells) for every transcribed row."""
    by_norm = {normalize_header(h): h for h in canonical}
    out: list[tuple[RowKind, dict[str, str], dict[str, str]]] = []
    for row in vp.rows:
        cells: dict[str, str] = {}
        for key, text in row.cells.items():
            mapped = canonical_key(key, vp.headers, canonical, by_norm)
            if mapped is not None and text.strip():
                cells[mapped] = text.strip()
        out.append((ROW_KINDS[row.row_kind], cells, dict(row.cells)))
    return out


def vision_table(pdf: Path, llm: LLMClient, *, source_file: str | None = None) -> RawTable:
    """Transcribe every page and assemble a RawTable (provenance is set by the extractor)."""
    pages = page_count(pdf)
    headers: list[str] = []
    rows: list[RawRow] = []
    by_page: dict[int, list[str]] = {}
    preamble: list[str] = []
    for page in range(1, pages + 1):
        vp = transcribe_page(pdf, page, llm)
        if not headers:
            if not vp.headers:
                logger.warning("page %d of %s returned no headers", page, pdf.name)
                continue
            headers = _unique(vp.headers)
        by_page[page] = list(headers)
        seen_table = False
        for kind, cells, raw in page_rows(vp, headers):
            if page == 1 and not seen_table and kind == "footnote":
                # Title lines above the table: kept as text so the as-of date can be found.
                text = " ".join(t.strip() for t in raw.values() if t.strip()) or " ".join(raw)
                if text.strip():
                    preamble.append(text.strip())
                continue
            if kind in ("unit", "header", "subtotal"):
                seen_table = True
            rows.append(
                RawRow(
                    page,
                    None,
                    len(rows),
                    kind,
                    {h: RawCell(text, None, None) for h, text in cells.items()},
                )
            )
    if not headers:
        raise VisionError(f"no table headers could be read from {pdf.name}")
    return RawTable(
        source_file=source_file or pdf.name,
        source_format="pdf_scanned",
        headers=headers,
        rows=rows,
        page_count=pages,
        header_texts_by_page=by_page,
        preamble_text="\n".join(preamble),
    )
