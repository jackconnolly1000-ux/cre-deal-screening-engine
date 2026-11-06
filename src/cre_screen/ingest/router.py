"""File and page type detection."""

from __future__ import annotations

from pathlib import Path

import pdfplumber

from cre_screen.errors import UnsupportedFileError
from cre_screen.schemas.common import SourceFormat

MIN_TEXT_CHARS = 20


def detect_format(path: Path) -> SourceFormat:
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return "xlsx"
    if suffix != ".pdf":
        raise UnsupportedFileError(f"unsupported file type {path.suffix!r}: {path.name}")
    thin = 0
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages:
            if len(page.chars) < MIN_TEXT_CHARS:
                thin += 1
            close = getattr(page, "close", None)
            if close:
                close()
    return "pdf_scanned" if total and thin > total / 2 else "pdf_text"
