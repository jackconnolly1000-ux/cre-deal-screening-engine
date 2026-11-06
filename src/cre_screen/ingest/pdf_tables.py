"""Text-layer PDF to RawTable using pdfplumber word coordinates.

All coordinates are PDF points with ``top`` measured from the page top.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

import pdfplumber

from cre_screen.config import RRHeadersConfig
from cre_screen.ingest.raw import RawCell, RawRow, RawTable, RowKind
from cre_screen.mapping.header_mapper import match_headers, normalize_header

logger = logging.getLogger(__name__)

LINE_TOLERANCE = 3.0
PHRASE_GAP = 6.0
MIN_HEADER_FIELDS = 2
UNIT_ID_LIKE = re.compile(r"^[A-Za-z]{0,3}[-#]?\d[\w-]{0,7}$")
SUBTOTAL_LIKE = re.compile(r"total|subtotal", re.IGNORECASE)
FOOTNOTE_LIKE = re.compile(r"^(\*|\d+\))")

Word = dict[str, Any]
Bbox = tuple[float, float, float, float]


def _group_lines(words: list[Word]) -> list[list[Word]]:
    lines: list[list[Word]] = []
    anchor = 0.0
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and word["top"] - anchor <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
            anchor = word["top"]
    for line in lines:
        line.sort(key=lambda w: w["x0"])
    return lines


def _merge_phrases(line: list[Word]) -> list[tuple[str, float, float]]:
    """Merge x-adjacent words (gap under PHRASE_GAP) into (text, x0, x1) phrases."""
    phrases: list[list[Any]] = []
    for word in line:
        if phrases and word["x0"] - phrases[-1][2] < PHRASE_GAP:
            phrases[-1][0] += " " + word["text"]
            phrases[-1][2] = word["x1"]
        else:
            phrases.append([word["text"], word["x0"], word["x1"]])
    return [(p[0], p[1], p[2]) for p in phrases]


def _find_header_line(
    lines: list[list[Word]], cfg: RRHeadersConfig
) -> tuple[int, list[tuple[str, float, float]]] | None:
    best: tuple[int, int, list[tuple[str, float, float]]] | None = None
    for i, line in enumerate(lines):
        phrases = _merge_phrases(line)
        score = len(match_headers([p[0] for p in phrases], cfg))
        if score >= MIN_HEADER_FIELDS and (best is None or score > best[0]):
            best = (score, i, phrases)
    return None if best is None else (best[1], best[2])


def _unique_headers(texts: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for text in texts:
        seen[text] = seen.get(text, 0) + 1
        out.append(text if seen[text] == 1 else f"{text} ({seen[text]})")
    return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _row_cells(
    line: list[Word], headers: list[str], bounds: list[tuple[float, float]]
) -> dict[str, RawCell]:
    per_col: dict[int, list[Word]] = {}
    for word in line:
        best_col = max(
            range(len(bounds)),
            key=lambda c: (_overlap(word["x0"], word["x1"], *bounds[c]), -c),
        )
        per_col.setdefault(best_col, []).append(word)
    cells: dict[str, RawCell] = {}
    for col in sorted(per_col):
        words = per_col[col]
        bbox: Bbox = (
            min(w["x0"] for w in words),
            min(w["top"] for w in words),
            max(w["x1"] for w in words),
            max(w["bottom"] for w in words),
        )
        cells[headers[col]] = RawCell(" ".join(w["text"] for w in words), bbox, None)
    return cells


def _classify(
    cells: dict[str, RawCell], headers: list[str], header_norms: list[str], is_repeat: bool
) -> RowKind:
    if not cells:
        return "blank"
    if is_repeat:
        return "header"
    first_header = next(h for h in headers if h in cells)
    first_text = cells[first_header].text
    if SUBTOTAL_LIKE.search(first_text):
        return "subtotal"
    if len(cells) == 1 and FOOTNOTE_LIKE.match(first_text):
        return "footnote"
    if first_header == headers[0] and UNIT_ID_LIKE.match(first_text):
        return "unit"
    return "footnote"


def _fingerprint(cfg: RRHeadersConfig) -> str:
    blob = json.dumps(
        {"fields": cfg.fields, "min": cfg.fuzzy_min_score}, sort_keys=True, ensure_ascii=True
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _extract(path: Path, cfg: RRHeadersConfig, source_file: str) -> RawTable:
    headers: list[str] = []
    header_norms: list[str] = []
    bounds: list[tuple[float, float]] = []
    by_page: dict[int, list[str]] = {}
    preamble: list[str] = []
    rows: list[RawRow] = []

    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
            lines = _group_lines(words)
            width = float(page.width)
            header_idx: int | None = None

            if not headers:
                found = _find_header_line(lines, cfg)
                if found is not None:
                    header_idx, phrases = found
                    headers = _unique_headers([p[0] for p in phrases])
                    header_norms = [normalize_header(h) for h in headers]
                    centers = [(p[1] + p[2]) / 2 for p in phrases]
                    edges = [(a + b) / 2 for a, b in pairwise(centers)]
                    starts = [0.0, *edges]
                    ends = [*edges, width]
                    bounds = list(zip(starts, ends, strict=True))
                    by_page[page_no] = list(headers)
                    preamble.extend(" ".join(w["text"] for w in ln) for ln in lines[:header_idx])
            else:
                for i, line in enumerate(lines):
                    texts = [normalize_header(p[0]) for p in _merge_phrases(line)]
                    if texts == header_norms:
                        header_idx = i
                        by_page[page_no] = list(headers)
                        break

            if not headers:
                logger.warning("no header line found on page %d of %s", page_no, source_file)
                close = getattr(page, "close", None)
                if close:
                    close()
                continue

            start = 0 if header_idx is None else header_idx
            for i in range(start, len(lines)):
                line = lines[i]
                is_repeat = i == header_idx
                cells = _row_cells(line, headers, bounds)
                kind = _classify(cells, headers, header_norms, is_repeat)
                rows.append(RawRow(page_no, None, len(rows), kind, cells))
            close = getattr(page, "close", None)
            if close:
                close()

    return RawTable(
        source_file=source_file,
        source_format="pdf_text",
        headers=headers,
        rows=rows,
        page_count=page_count,
        header_texts_by_page=by_page,
        preamble_text="\n".join(preamble),
    )


def extract_pdf_table(
    path: Path,
    cfg: RRHeadersConfig,
    *,
    cache_dir: Path | None = None,
    source_file: str | None = None,
) -> RawTable:
    """Extract the main table from a text-layer PDF.

    Pure with respect to the file. With ``cache_dir`` the result is cached as
    ``<sha256 of file>.json`` (the entry also records a fingerprint of the header config, and is
    ignored when it no longer matches).
    """
    name = source_file or path.name
    cache_path: Path | None = None
    fingerprint = _fingerprint(cfg)
    if cache_dir is not None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        cache_path = cache_dir / f"{digest}.json"
        if cache_path.exists():
            entry = json.loads(cache_path.read_text(encoding="utf-8"))
            if entry.get("fingerprint") == fingerprint and entry["table"]["source_file"] == name:
                return RawTable.from_json_dict(entry["table"])

    table = _extract(path, cfg, name)
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        payload = {"fingerprint": fingerprint, "table": table.to_json_dict()}
        tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        os.replace(tmp, cache_path)
    return table
