"""Parse DocILE per-document annotation JSON into the KILE shape that
Phase 4 cascade work consumes.

Upstream annotation files (``<root>/annotations/<doc_id>.json``) carry
four top-level blocks: ``field_extractions`` (KILE — the 55-field
taxonomy we use), ``line_item_extractions`` (LIR — out of scope per
the Phase 3.5 KILE-only locked decision), ``line_item_headers`` (also
LIR), and ``metadata`` (document-level info: page count, page sizes,
language, document type, source, etc.).

The parser surfaces only what downstream stages need:
  - ``DocileField``: one KILE extraction (page, bbox, fieldtype, text).
  - ``DocileDocument``: doc_id + split + page_count + per-page sizes +
    tuple of KILE fields.

Page indexing convention: upstream uses **0-indexed** pages everywhere
(``field_extractions[*].page``, the per-page entries in
``page_sizes_at_200dpi``, etc.). The parser keeps that 0-indexed
convention so structured output round-trips cleanly with upstream
tooling. The sidecar builder (Task #4) converts to the
**1-indexed** ``page.number`` that the existing CMS-1500 renderer
sidecar emits.

BBox convention: normalized floats in ``[0, 1]``, ``(left, top, right,
bottom)``, top-left origin. Same as the upstream ``BBox`` dataclass.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

KILE_BLOCK = "field_extractions"
"""Top-level key in the annotation JSON carrying the KILE annotations."""

METADATA_BLOCK = "metadata"
"""Top-level key carrying document-level metadata (page sizes, etc.)."""

ALLOWED_SPLITS = frozenset(["train", "val"])

#: Tolerance for clamping bbox coordinates that fall marginally outside the
#: normalized [0, 1] range. Upstream DocILE KILE annotations carry sub-pixel
#: rounding noise (observed e.g. ``top = -0.00236`` on
#: ``ac61eb8c9ab94f579196f7ab``) — a rasterization artifact, not corrupt
#: data. Clamping these to [0, 1] is what DocILE's own tooling does; aborting
#: the whole 500-doc ingest on a 0.002 overshoot is wrong. Coordinates
#: outside ``[-tol, 1+tol]`` are still genuine corruption and fail loudly.
_BBOX_CLAMP_TOL = 0.02
"""Splits Phase 3.5 may iterate. The ``test`` split is reserved for the
Phase 7 ``process-batch`` recipe per the half-now-half-later
partitioning lock in ``cost-model.md``."""


@dataclass(frozen=True)
class DocileField:
    """One KILE annotation: a page-located field with a typed label.

    ``page`` is 0-indexed (upstream convention). ``bbox`` is normalized
    to the page's dimensions (each component in ``[0, 1]``, top-left
    origin, ``(left, top, right, bottom)``). ``text`` carries the OCR'd
    string the annotation labels and may be ``None`` for blank-field
    annotations (rare in practice for KILE ground truth).
    """

    page: int
    bbox: tuple[float, float, float, float]
    fieldtype: str
    text: str | None


@dataclass(frozen=True)
class DocileDocument:
    """KILE view of a DocILE document.

    ``page_sizes_at_200dpi`` is the per-page ``(width_px, height_px)``
    tuple from the upstream metadata. DocILE rasterizes its OCR layer
    at 200 DPI, so these are the pixel dimensions our rasterizer
    targets to keep bbox coordinates round-trip-clean between
    normalized and pixel space.
    """

    doc_id: str
    split: str
    page_count: int
    page_sizes_at_200dpi: tuple[tuple[int, int], ...]
    fields: tuple[DocileField, ...]


def _validate_bbox(raw: object, where: str) -> tuple[float, float, float, float]:
    """Coerce a bbox list/tuple to a 4-float tuple, validate shape + range."""
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        raise ValueError(f"{where}: bbox must be a 4-element list, got {raw!r}")
    coords: list[float] = []
    for i, v in enumerate(raw):
        if not isinstance(v, int | float):
            raise ValueError(f"{where}: bbox[{i}] must be numeric, got {v!r}")
        coords.append(float(v))
    # Clamp benign sub-pixel upstream annotation noise into [0, 1]; reject
    # only genuinely corrupt coordinates (beyond _BBOX_CLAMP_TOL).
    for i, v in enumerate(coords):
        if v < -_BBOX_CLAMP_TOL or v > 1.0 + _BBOX_CLAMP_TOL:
            raise ValueError(
                f"{where}: bbox[{i}]={v} out of normalized [0,1] range "
                f"(beyond ±{_BBOX_CLAMP_TOL} clamp tolerance) — corrupt annotation"
            )
    left, top, right, bottom = (min(1.0, max(0.0, c)) for c in coords)
    if not (left <= right) or not (top <= bottom):
        raise ValueError(
            f"{where}: bbox {coords} has left>right / top>bottom "
            f"(clamped to ({left}, {top}, {right}, {bottom}))"
        )
    return (left, top, right, bottom)


def _parse_field(raw: object, where: str) -> DocileField:
    """Build a DocileField from one ``field_extractions`` entry."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: field entry must be a dict, got {type(raw).__name__}")

    page = raw.get("page")
    if not isinstance(page, int) or page < 0:
        raise ValueError(f"{where}: page must be a non-negative int, got {page!r}")

    fieldtype = raw.get("fieldtype")
    if not isinstance(fieldtype, str) or not fieldtype:
        raise ValueError(f"{where}: fieldtype must be a non-empty string, got {fieldtype!r}")

    text = raw.get("text")
    if text is not None and not isinstance(text, str):
        raise ValueError(f"{where}: text must be a string or null, got {type(text).__name__}")

    bbox = _validate_bbox(raw.get("bbox"), f"{where}.bbox")

    return DocileField(page=page, bbox=bbox, fieldtype=fieldtype, text=text)


def _parse_page_sizes(raw: object, page_count: int, where: str) -> tuple[tuple[int, int], ...]:
    """Coerce ``page_sizes_at_200dpi`` to a tuple of (w, h) int pairs."""
    if not isinstance(raw, list):
        raise ValueError(f"{where}: page_sizes_at_200dpi must be a list, got {raw!r}")
    if len(raw) != page_count:
        raise ValueError(
            f"{where}: page_sizes_at_200dpi length {len(raw)} != page_count {page_count}"
        )
    sizes: list[tuple[int, int]] = []
    for i, pair in enumerate(raw):
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            raise ValueError(f"{where}: page_sizes_at_200dpi[{i}] must be a 2-list, got {pair!r}")
        w, h = pair
        if not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
            raise ValueError(
                f"{where}: page_sizes_at_200dpi[{i}] must be (positive int, positive int), "
                f"got ({w!r}, {h!r})"
            )
        sizes.append((w, h))
    return tuple(sizes)


def load_document(annotation_path: Path, split: str) -> DocileDocument:
    """Parse one DocILE annotation file into a ``DocileDocument``.

    ``annotation_path`` points at a single ``<doc_id>.json`` under the
    upstream ``annotations/`` directory. ``doc_id`` is inferred from
    the filename stem. ``split`` must be ``"train"`` or ``"val"``;
    ``"test"`` is rejected per the half-now-half-later lock.
    """
    if split not in ALLOWED_SPLITS:
        raise ValueError(
            f"split must be one of {sorted(ALLOWED_SPLITS)} for Phase 3.5; "
            f"got {split!r}. The 'test' split is reserved for the Phase 7 "
            f"process-batch recipe per the half-now-half-later partitioning lock."
        )

    annotation_path = Path(annotation_path)
    doc_id = annotation_path.stem
    raw = json.loads(annotation_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{annotation_path}: top-level JSON must be an object")

    metadata = raw.get(METADATA_BLOCK)
    if not isinstance(metadata, dict):
        raise ValueError(f"{annotation_path}: missing or non-dict 'metadata' block")

    page_count = metadata.get("page_count")
    if not isinstance(page_count, int) or page_count <= 0:
        raise ValueError(
            f"{annotation_path}: metadata.page_count must be a positive int, got {page_count!r}"
        )

    page_sizes = _parse_page_sizes(
        metadata.get("page_sizes_at_200dpi"),
        page_count,
        where=f"{annotation_path}.metadata",
    )

    field_block = raw.get(KILE_BLOCK, [])
    if not isinstance(field_block, list):
        raise ValueError(f"{annotation_path}: '{KILE_BLOCK}' must be a list, got {field_block!r}")

    fields = tuple(
        _parse_field(entry, where=f"{annotation_path}.{KILE_BLOCK}[{i}]")
        for i, entry in enumerate(field_block)
    )

    # Per-field page index must fall within the document's pages.
    for i, field in enumerate(fields):
        if field.page >= page_count:
            raise ValueError(
                f"{annotation_path}.{KILE_BLOCK}[{i}].page={field.page} "
                f">= page_count={page_count}"
            )

    return DocileDocument(
        doc_id=doc_id,
        split=split,
        page_count=page_count,
        page_sizes_at_200dpi=page_sizes,
        fields=fields,
    )


def iter_split(dataset_root: Path, split: str) -> Iterator[tuple[str, Path]]:
    """Yield ``(doc_id, annotation_path)`` for every doc in a split.

    Reads ``<dataset_root>/<split>.json`` (a JSON list of doc_ids) and
    pairs each id with ``<dataset_root>/annotations/<doc_id>.json``.
    Annotation files must exist — a missing file raises
    ``FileNotFoundError``, since the upstream archives are atomic
    bundles and a referenced-but-absent annotation indicates a
    truncated or corrupted extraction.
    """
    if split not in ALLOWED_SPLITS:
        raise ValueError(
            f"split must be one of {sorted(ALLOWED_SPLITS)} for Phase 3.5; "
            f"got {split!r}. The 'test' split is reserved for the Phase 7 "
            f"process-batch recipe per the half-now-half-later partitioning lock."
        )

    dataset_root = Path(dataset_root)
    split_index_path = dataset_root / f"{split}.json"
    if not split_index_path.is_file():
        raise FileNotFoundError(
            f"Split index {split_index_path} not found. Did "
            f"download_labeled_trainval() succeed against this dest_dir?"
        )

    doc_ids = json.loads(split_index_path.read_text(encoding="utf-8"))
    if not isinstance(doc_ids, list):
        raise ValueError(
            f"{split_index_path}: split index must be a JSON list of doc_ids, "
            f"got {type(doc_ids).__name__}"
        )

    annotations_dir = dataset_root / "annotations"
    for doc_id in doc_ids:
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError(
                f"{split_index_path}: doc_ids must be non-empty strings, got {doc_id!r}"
            )
        annotation_path = annotations_dir / f"{doc_id}.json"
        if not annotation_path.is_file():
            raise FileNotFoundError(
                f"Annotation file missing: {annotation_path} " f"(referenced by {split_index_path})"
            )
        yield doc_id, annotation_path
