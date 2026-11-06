"""PDF page rendering. The only module that imports PyMuPDF.

PyMuPDF is licensed AGPL-3.0, which is why this project is AGPL. ``pypdfium2`` (BSD/Apache) can
replace it behind the same four functions. PyMuPDF 2.x deprecates the ``fitz`` name in favor of
``pymupdf``; the alias below keeps the rest of the repo unaware of that.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw

PDF_POINTS_PER_INCH = 72


def page_count(pdf: Path) -> int:
    with fitz.open(pdf) as doc:
        return doc.page_count


def render_page_png(pdf: Path, page: int, dpi: int = 110) -> bytes:
    """Render a 1-based page to PNG bytes."""
    with fitz.open(pdf) as doc:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def draw_boxes(png: bytes, boxes: list[tuple[float, float, float, float]], scale: float) -> bytes:
    """Draw rectangles given in PDF points (x0, top, x1, bottom); scale = dpi / 72."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(image)
    for x0, top, x1, bottom in boxes:
        draw.rectangle(
            [x0 * scale, top * scale, x1 * scale, bottom * scale], outline="red", width=2
        )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def rasterize_pdf(pdf: Path, out: Path, dpi: int) -> None:
    """Replace every page with an image-only page at ``dpi`` (used by synth/degrade.py)."""
    with fitz.open(pdf) as src, fitz.open() as dst:
        for page in src:
            pix = page.get_pixmap(dpi=dpi)
            new_page = dst.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, pixmap=pix)
        tmp = out.with_name(out.name + ".tmp")
        dst.save(tmp)
    tmp.replace(out)
