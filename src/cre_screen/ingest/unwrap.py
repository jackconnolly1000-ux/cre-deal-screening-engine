"""Rejoin words that a narrow table column wrapped onto neighbouring lines.

Some renderers break a long cell across lines, even in the middle of a word, so the text layer
holds ``Uni`` above ``t``, a date as ``04/03/2`` above ``6``, or an amount as ``$110,95`` above
``6``. The fragments sit a few points above and below the row's main line, which is closer than
normal row spacing but farther than the line tolerance of the table reader.

``unwrap_words`` finds those bands of closely stacked lines and merges every stack of fragments
back into one word at the main line's height. Coordinates stay in PDF points with ``top``
measured from the page top. The merged word keeps the union box in ``box_top`` and
``box_bottom`` so citations still cover the printed fragments.

This pass is only used after a plain read of the page has failed, so it cannot change how a
table that already parses is read.
"""

from __future__ import annotations

import re
from statistics import median
from typing import Any

Word = dict[str, Any]

MIN_OVERLAP = 0.25  # share of the narrower word that must overlap to count as stacked
EDGE_SLACK = 2.5  # left or right edges this close count as aligned
GLUE_MAX_LEN = 3
NUMERIC_END = re.compile(r"[\d,./$%-]$")
NUMERIC_START = re.compile(r"^[\d,./%)-]")


def _lines(words: list[Word], tolerance: float) -> list[list[Word]]:
    lines: list[list[Word]] = []
    anchor = 0.0
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and word["top"] - anchor <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
            anchor = word["top"]
    return lines


def _stacked(a: Word, b: Word) -> bool:
    """True when two words are in the same column: their x-ranges overlap or an edge lines up."""
    overlap = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    narrower = min(a["x1"] - a["x0"], b["x1"] - b["x0"])
    if narrower > 0 and overlap / narrower >= MIN_OVERLAP:
        return True
    return abs(a["x0"] - b["x0"]) <= EDGE_SLACK or abs(a["x1"] - b["x1"]) <= EDGE_SLACK


def join_fragments(upper: str, lower: str) -> str:
    """Glue two stacked fragments: no space inside a number, date or split word, else a space."""
    if NUMERIC_END.search(upper) and NUMERIC_START.match(lower):
        return upper + lower
    if lower.isalpha() and lower.islower() and len(lower) <= GLUE_MAX_LEN and upper[-1:].isalpha():
        return upper + lower
    return f"{upper} {lower}"


def _union(words: list[Word], text: str, top: float) -> Word:
    return {
        "text": text,
        "x0": min(w["x0"] for w in words),
        "x1": max(w["x1"] for w in words),
        "top": top,
        "bottom": top + (words[0]["bottom"] - words[0]["top"]),
        "box_top": min(w["top"] for w in words),
        "box_bottom": max(w["bottom"] for w in words),
    }


def _merge_band(band: list[list[Word]]) -> list[Word]:
    main = max(range(len(band)), key=lambda i: (len(band[i]), -i))
    anchors = [dict(w) for w in band[main]]
    # words of the other lines, nearest line first so a stack grows outward from the main line
    others = [(i, w) for i, line in enumerate(band) if i != main for w in line]
    stacks: list[list[tuple[int, Word]]] = [[(main, a)] for a in anchors]
    loose: list[list[tuple[int, Word]]] = []
    for i, word in sorted(others, key=lambda iw: (abs(iw[0] - main), iw[0], iw[1]["x0"])):
        home = next((s for s in stacks + loose if any(_stacked(word, m) for _, m in s)), None)
        if home is None:
            loose.append([(i, word)])
        else:
            home.append((i, word))
    top = band[main][0]["top"]
    merged: list[Word] = []
    for stack in stacks + loose:
        ordered = sorted(stack, key=lambda iw: (iw[0], iw[1]["x0"]))
        text = ordered[0][1]["text"]
        for _, w in ordered[1:]:
            text = join_fragments(text, w["text"])
        merged.append(_union([w for _, w in ordered], text, top))
    return merged


def unwrap_words(words: list[Word]) -> list[Word]:
    """Return ``words`` with wrapped fragments merged; words that were not wrapped pass through."""
    if len(words) < 2:
        return words
    heights = [w["bottom"] - w["top"] for w in words]
    gap = median(heights)  # stacked fragments sit closer than one text height
    lines = _lines(words, tolerance=1.0)
    bands: list[list[list[Word]]] = []
    for line in lines:
        top = min(w["top"] for w in line)
        if bands and top - min(w["top"] for w in bands[-1][-1]) <= gap:
            bands[-1].append(line)
        else:
            bands.append([line])
    out: list[Word] = []
    for band in bands:
        out.extend(band[0] if len(band) == 1 else _merge_band(band))
    return out
