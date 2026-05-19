"""Build per-page sidecar JSON for a DocILE-rasterized PNG.

The DocILE ingestion path produces (PNG, sidecar) pairs that the
``synthetic_data.render.upload`` module copies into the local
content-addressable store under the same scheme as the CMS-1500
renderer (V1 is local-first; V2 restores the S3 path). The sidecar
schema (v1, locked) requires top-level ``image_sha256`` +
``source_id`` + ``page`` so the generic store writer can route any
vertical's pairs without branching. DocILE-specific annotations (the
KILE taxonomy for this page) live under a ``docile`` namespace so the
top-level schema stays portable.

The sidecar is **per page**, not per document: a 3-page PDF produces
3 (PNG, sidecar) pairs, each with its own ``image_sha256`` and
``source_id = "<doc_id>-p<page_number>"``. KILE annotations are
filtered to the page they belong to (upstream's 0-indexed
``page`` attribute converted to our 1-indexed page_number).
"""

from __future__ import annotations

from synthetic_data.docile.parse import DocileDocument
from synthetic_data.docile.rasterize import RasterizedPage

SIDECAR_SCHEMA_VERSION = 1
"""Sidecar schema version. Matches the v1 contract the renderer +
store writer already agree on; bump in lockstep with
``synthetic_data.render.upload.SIDECAR_SCHEMA_VERSION_SUPPORTED`` only
on a deliberate breaking change."""


def build_docile_sidecar(
    doc: DocileDocument,
    page: RasterizedPage,
    image_sha256: str,
) -> dict:
    """Compose the sidecar JSON dict for one rasterized DocILE page.

    ``page.page_number`` is 1-indexed (the renderer/sidecar convention);
    KILE annotations in ``doc.fields`` carry an upstream-faithful
    0-indexed ``page`` attribute. Conversion happens here, exactly
    once: we keep annotations whose ``field.page + 1 ==
    page.page_number``.

    ``source_id`` follows the pattern ``"<doc_id>-p<page_number>"`` so
    each page has a unique source identifier suitable for the S3
    metadata stamp; the docs.doc_id and the page number are also
    preserved verbatim under ``docile.{doc_id,page_number}`` for
    direct programmatic access without parsing the source_id slug.
    """
    page_fields = [
        {
            "fieldtype": f.fieldtype,
            "bbox": list(f.bbox),
            "text": f.text,
        }
        for f in doc.fields
        if f.page + 1 == page.page_number
    ]
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "image": page.png_path.name,
        "image_sha256": image_sha256,
        "source_id": f"{doc.doc_id}-p{page.page_number}",
        "page": {
            "number": page.page_number,
            "width_px": page.width_px,
            "height_px": page.height_px,
        },
        "docile": {
            "doc_id": doc.doc_id,
            "split": doc.split,
            "page_number": page.page_number,
            "fields": page_fields,
        },
    }
