"""End-to-end DocILE ingestion orchestrator.

Ties together the parse + rasterize + sidecar steps into a single
function that prepares a render directory of (PNG, sidecar) pairs.
Does NOT upload — the caller invokes
``synthetic_data.render.upload.upload_render_dir`` on the prepared
directory once ingestion is complete. Keeping ingest separate from
upload mirrors the CMS-1500 ``render_batch`` → ``upload_render_dir``
shape and keeps the S3 surface untouched in pure-function ingestion
tests.

A single render directory holds every doc's pages flat:
``<render_dir>/<doc_id>-p<N>.{png,json}``. The same shape
``find_render_pairs`` already expects from the CMS-1500 renderer — so
the uploader needs zero DocILE-specific code paths.

Default S3 prefix is ``synthetic/business/docile`` — mirrors the
healthcare ``synthetic/healthcare/cms1500`` prefix one directory level
up, keeping a clean per-vertical namespace on the documents bucket.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from synthetic_data.docile.parse import (
    ALLOWED_SPLITS,
    DocileDocument,
    iter_split,
    load_document,
)
from synthetic_data.docile.rasterize import RasterizedPage, rasterize_document
from synthetic_data.docile.sidecar import build_docile_sidecar

DEFAULT_S3_PREFIX_DOCILE = "synthetic/business/docile"
"""S3 prefix DocILE artifacts land under in the documents bucket.

Parallel to ``synthetic_data.render.upload.DEFAULT_S3_PREFIX`` for
healthcare (``synthetic/healthcare/cms1500``). The two-level
``synthetic/<vertical>/<source>`` shape keeps the bucket browsable by
vertical."""


def ingest_document(
    doc: DocileDocument,
    pdf_path: Path,
    render_dir: Path,
) -> list[RasterizedPage]:
    """Rasterize one doc's PDF and write per-page sidecar JSONs.

    Returns the list of ``RasterizedPage`` for the document. The
    ``render_dir`` accumulates pairs from multiple docs — each doc's
    pages are uniquely named by ``<doc_id>-p<N>``, so docs don't
    collide. Caller is responsible for invoking the uploader once all
    docs are ingested.

    Idempotent at the page level: re-rasterizing overwrites the same
    PNG bytes (deterministic given the same pypdfium2 version), so a
    half-finished run can resume by re-invoking the same call.

    Raises ``ValueError`` if the PDF's actual page count disagrees
    with ``doc.page_count`` (which comes from the upstream annotation
    metadata). A mismatch indicates corrupted data — silently emitting
    extra/missing sidecars would leak the inconsistency downstream.
    """
    render_dir.mkdir(parents=True, exist_ok=True)
    pages = rasterize_document(pdf_path, render_dir, doc.doc_id)
    if len(pages) != doc.page_count:
        raise ValueError(
            f"Page count mismatch for {doc.doc_id}: PDF rasterized "
            f"{len(pages)} page(s), annotation metadata declares "
            f"{doc.page_count}. Refusing to write inconsistent sidecars."
        )
    for page in pages:
        image_sha256 = hashlib.sha256(page.png_path.read_bytes()).hexdigest()
        sidecar = build_docile_sidecar(doc, page, image_sha256)
        sidecar_path = page.png_path.with_suffix(".json")
        sidecar_path.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return pages


def ingest_dataset(
    dataset_root: Path,
    render_dir: Path,
    *,
    splits: tuple[str, ...] = ("train", "val"),
    limit: int | None = None,
) -> int:
    """Process every doc in ``splits`` into ``render_dir``.

    ``dataset_root`` is the directory that ``download_labeled_trainval``
    extracted to: ``<root>/annotations/`` + ``<root>/pdfs/`` + the
    per-split index files ``<root>/{train,val}.json``.

    ``splits`` defaults to ``("train", "val")`` — the half-now-half-later
    lock forbids ``"test"`` and ``iter_split`` enforces that, but we
    also reject it here for defense-in-depth.

    ``limit`` caps document count (5-10 is the recommended smoke;
    ``None`` or ``0`` means process everything — the latter lets the
    ``just`` recipe pass an unconditional ``--limit {{limit}}`` with
    a sentinel default rather than conditionally building the flag).
    Negative values are rejected with ``ValueError`` rather than
    treated as a no-cap synonym, so a typo in the recipe surfaces
    immediately. Counts documents, not pages, so a ``limit=5``
    against multi-page docs yields more than 5 (PNG, sidecar) pairs.

    Returns the number of documents processed.
    """
    for split in splits:
        if split not in ALLOWED_SPLITS:
            raise ValueError(
                f"ingest_dataset: split {split!r} not in {sorted(ALLOWED_SPLITS)}. "
                f"The 'test' split is reserved for the Phase 7 process-batch "
                f"recipe per the half-now-half-later partitioning lock."
            )

    if limit is not None and limit < 0:
        raise ValueError(
            f"ingest_dataset: limit must be non-negative (None or 0 means no cap), " f"got {limit}"
        )

    dataset_root = Path(dataset_root)
    pdfs_dir = dataset_root / "pdfs"

    cap = limit if (limit is not None and limit > 0) else None

    processed = 0
    for split in splits:
        for doc_id, annotation_path in iter_split(dataset_root, split):
            if cap is not None and processed >= cap:
                return processed
            pdf_path = pdfs_dir / f"{doc_id}.pdf"
            if not pdf_path.is_file():
                raise FileNotFoundError(
                    f"PDF missing: {pdf_path} (annotation present at {annotation_path})"
                )
            doc = load_document(annotation_path, split=split)
            ingest_document(doc, pdf_path, render_dir)
            processed += 1

    return processed


def main(argv: list[str] | None = None) -> int:
    """Ingest DocILE annotated documents into a render directory.

    Does NOT upload. Pipe the resulting render directory into
    ``python -m synthetic_data.render.upload --prefix
    synthetic/business/docile`` to get the pairs to S3 (the
    ``synthetic-data-docile-build`` ``just`` recipe chains the two).

    Usage::

        uv run python -m synthetic_data.docile.ingest \\
            --dataset-root synthetic_data/output/docile \\
            --render-dir synthetic_data/output/docile/render \\
            --limit 5
    """
    parser = argparse.ArgumentParser(
        description=(
            "Rasterize DocILE PDFs to per-page PNGs + write KILE-annotated "
            "sidecar JSONs into a render directory."
        ),
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        required=True,
        help="DocILE extracted root (annotations/, pdfs/, train.json, val.json).",
    )
    parser.add_argument(
        "--render-dir",
        type=Path,
        required=True,
        help="Output directory for the (PNG, sidecar) pairs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help=(
            "Cap on number of documents to ingest. 0 (default) means no cap; "
            "use e.g. --limit 5 for a smoke run."
        ),
    )
    args = parser.parse_args(argv)

    if not args.dataset_root.is_dir():
        print(f"--dataset-root {args.dataset_root} is not a directory", file=sys.stderr)
        return 2

    processed = ingest_dataset(args.dataset_root, args.render_dir, limit=args.limit)
    print(f"Ingested {processed} document(s) -> {args.render_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
