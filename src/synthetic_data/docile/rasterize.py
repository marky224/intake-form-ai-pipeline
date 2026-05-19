"""Rasterize DocILE PDFs to per-page PNG images via pypdfium2.

DocILE ships PDFs alongside its annotations; the cascade pipeline
operates on PNG inputs (per the locked Phase 3 output spec — every
renderer/ingestion path produces PNG so the cascade evaluation runs
through the same OCR boundary as production scans). This module
rasterizes one PDF into one PNG per page at 200 DPI, matching the
upstream metadata's ``page_sizes_at_200dpi`` so DocILE's normalized
bboxes round-trip cleanly between normalized and pixel space.

Page-numbering convention: the rasterizer writes ``<doc_id>-p<N>.png``
with **N = 1-indexed page number** (matching the CMS-1500 renderer's
sidecar ``page.number`` convention). The DocILE annotation parser
keeps the upstream 0-indexed ``page`` attribute; conversion happens
exactly once, at sidecar build time (Task #4), so the parser's
upstream-faithful contract is preserved and downstream consumers
see a single consistent indexing scheme on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

RASTERIZE_DPI = 200
"""Target rasterization DPI. Matches DocILE's
``metadata.page_sizes_at_200dpi`` so a page rendered at this scale
has dimensions equal to the metadata's per-page (width_px, height_px),
making bbox de-normalization exact rather than off-by-rounding."""

PDF_POINTS_PER_INCH = 72
"""PDF's coordinate system is points (1/72 inch). pypdfium2's
``page.render(scale=...)`` takes a unitless scale factor from points
to pixels; ``RASTERIZE_DPI / PDF_POINTS_PER_INCH`` is the right
multiplier."""


@dataclass(frozen=True)
class RasterizedPage:
    """One PNG produced for one PDF page.

    ``page_number`` is 1-indexed for downstream consistency with the
    CMS-1500 renderer's sidecar shape. ``width_px``/``height_px`` are
    the actual rasterized dimensions — convenient for the sidecar
    builder so it doesn't re-open the PNG to read them.
    """

    page_number: int
    png_path: Path
    width_px: int
    height_px: int


def rasterize_document(
    pdf_path: Path,
    out_dir: Path,
    doc_id: str,
    *,
    dpi: int = RASTERIZE_DPI,
) -> list[RasterizedPage]:
    """Rasterize every page of ``pdf_path`` into PNG files in ``out_dir``.

    Returns one ``RasterizedPage`` per page, in page order. Output
    file names follow ``<doc_id>-p<page_number>.png`` (1-indexed). The
    output directory is created if it doesn't exist. Existing files
    with matching names are overwritten — the rasterizer is idempotent
    in its output, not its filesystem (re-running produces the same
    bytes from the same input).

    The PDF is closed before returning; pypdfium2's native resources
    don't leak.
    """
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / PDF_POINTS_PER_INCH

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        results: list[RasterizedPage] = []
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            page_number = page_index + 1
            png_path = out_dir / f"{doc_id}-p{page_number}.png"
            pil_image.save(png_path, format="PNG")
            results.append(
                RasterizedPage(
                    page_number=page_number,
                    png_path=png_path,
                    width_px=pil_image.width,
                    height_px=pil_image.height,
                )
            )
        return results
    finally:
        pdf.close()
