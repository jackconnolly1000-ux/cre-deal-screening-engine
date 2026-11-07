"""Scanned-page simulation (SPEC 11.3).

Rasterize at 150 dpi, rotate each page by a seeded angle in +/-1.5 degrees, add Gaussian noise
(sigma 6), save as JPEG quality 70, and rewrap as an image-only PDF. Everything random comes from
one ``random.Random(seed)`` so the same seed always gives the same scan.
"""

from __future__ import annotations

import io
from pathlib import Path
from random import Random

from PIL import Image, ImageChops

from cre_screen.ingest.render import images_to_pdf, page_count, page_size, render_page_png

DPI = 150
MAX_ANGLE = 1.5
NOISE_SIGMA = 6.0
JPEG_QUALITY = 70
PAPER = 255


def _noise_layer(size: tuple[int, int], rng: Random) -> Image.Image:
    """Gaussian noise centred on 128, so ImageChops.add(..., offset=-128) adds it signed."""
    count = size[0] * size[1]
    data = bytes(max(0, min(255, round(128 + rng.gauss(0.0, NOISE_SIGMA)))) for _ in range(count))
    return Image.frombytes("L", size, data)


def degrade_page(png: bytes, rng: Random) -> bytes:
    """One page: rotate, add noise, return JPEG bytes."""
    image = Image.open(io.BytesIO(png)).convert("L")
    angle = rng.uniform(-MAX_ANGLE, MAX_ANGLE)
    image = image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=PAPER)
    image = ImageChops.add(image, _noise_layer(image.size, rng), scale=1.0, offset=-128)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def degrade_pdf(source: Path, out: Path, seed: int) -> None:
    """Replace a text-layer PDF with a scanned-looking image-only PDF at ``out``."""
    rng = Random(seed)
    pages = page_count(source)
    jpegs = [degrade_page(render_page_png(source, p, dpi=DPI), rng) for p in range(1, pages + 1)]
    sizes = [page_size(source, p) for p in range(1, pages + 1)]
    images_to_pdf(jpegs, sizes, out)
