"""PDF page rendering. The only module that imports PyMuPDF.

PyMuPDF is licensed AGPL-3.0, which is why this project is AGPL. ``pypdfium2`` (BSD/Apache) can
replace it behind the same functions. PyMuPDF 2.x deprecates the ``fitz`` name in favor of
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


def page_image(pdf: Path, page: int, dpi: int = 110) -> tuple[bytes, str]:
    """Image bytes and media type to show a vision model for a 1-based page.

    A scanned page is a single full-page image with no text layer. Its embedded image is returned
    exactly as stored: lossless, and byte-identical on every OS, so the replay cache key (which
    hashes the image) does not depend on how a platform decodes JPEGs or compresses PNGs. Any
    other page is rendered to PNG at ``dpi``.
    """
    with fitz.open(pdf) as doc:
        target = doc[page - 1]
        images = target.get_images(full=True)
        if len(images) == 1 and not target.get_text().strip():
            info = doc.extract_image(images[0][0])
            ext = str(info.get("ext", "")).lower()
            if ext in ("jpeg", "jpg"):
                return bytes(info["image"]), "image/jpeg"
            if ext == "png":
                return bytes(info["image"]), "image/png"
        return target.get_pixmap(dpi=dpi).tobytes("png"), "image/png"


def images_to_pdf(jpegs: list[bytes], page_sizes: list[tuple[float, float]], out: Path) -> None:
    """Write an image-only PDF: one full-page JPEG per page (used by synth/degrade.py)."""
    with fitz.open() as doc:
        for data, (width, height) in zip(jpegs, page_sizes, strict=True):
            new_page = doc.new_page(width=width, height=height)
            new_page.insert_image(new_page.rect, stream=data)
        tmp = out.with_name(out.name + ".tmp")
        doc.save(tmp, deflate=True, garbage=3, no_new_id=True)
    tmp.replace(out)


def page_size(pdf: Path, page: int) -> tuple[float, float]:
    """(width, height) in PDF points of a 1-based page."""
    with fitz.open(pdf) as doc:
        rect = doc[page - 1].rect
        return float(rect.width), float(rect.height)
