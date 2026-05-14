"""Tier 1 PaddleOCR-VL local provider.

Pinned model: **PaddleOCR-VL-1.5** (released 2026-01-29; OmniDocBench v1.5 =
94.5%; irregular-shaped bbox localization; seal recognition; robust to scan
skew/warping/screen-photography/complex illumination). Pulled via the
``paddleocr`` Python package — bumping the package version is what pins the
checkpoint, since ``PaddleOCRVL()`` constructs from the package's bundled
weights.

**Device pin:** ``gpu:1`` (RTX 4060 Ti, 16 GB). GPU 0 (RTX 4080, 16 GB) is
reserved for Tier 2 Qwen 2.5 VL 7B (V1) / Tier 3 Qwen 2.5 VL 32B so the
cascade can hold the larger models resident across a batch without paging.

**Role in the cascade:** cheap-and-fast first-pass extraction, sub-second per
page. High precision on the easy 60-70% of fields. Lower-confidence fields
escalate to Tier 2 per the Phase 5 orchestrator's confidence threshold
(0.85 — locked, tuned in Phase 6).

**Cost:** $0.00/call (local inference).

**Architectural note: PaddleOCR-VL is a layout parser, NOT a prompted VL
model.** ``pipeline.predict(image)`` returns ``parsing_res_list`` — a list of
layout blocks ``{block_bbox, block_label, block_content}`` — and ignores any
``prompt`` argument. The "VL" suffix is misleading; PaddleOCR-VL is positioned
as PP-Structure-V3's VL-trained successor (more robust OCR), not a Qwen-VL-
style prompt-and-extract model. Field-name extraction is therefore a downstream
step inside this provider: each ``block_content`` is matched against the
project's ``alias_table_seed.json`` (~465 aliases across 86 records) to map
recognized labels to canonical field names. The locked ``extract(png,
form_cls) → ProviderResult[T]`` Protocol shape stays intact — Phase 5's
orchestrator and the downstream Tier 2 / Tier 3 providers don't need to know
that Tier 1's "field-by-field JSON" is synthesized post-OCR rather than
prompted out of the model.

**Lazy imports:** ``paddle`` and ``paddleocr`` are imported inside
``_load_paddleocr_vl_pipeline`` rather than at module top-level so the
``cascade`` package loads cleanly on machines without the GPU stack (e.g.,
CI). Cached-replay tests never touch the live path; only the
``EVAL_LIVE=true`` workflow on the build machine pays the import + load cost.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

from cascade import eval_cache
from cascade.providers._base import ProviderResult, T
from intake_schemas import (
    BoundingBox,
    ExtractedField,
    FormMetadata,
    TierId,
    get_field_metadata,
)

#: Stable provider identifier. Used as the eval-cache subdirectory name
#: (``tests/fixtures/eval-cache/tier1_paddleocr_local/<sha>.json``).
PROVIDER_NAME = "tier1_paddleocr_local"

#: Cascade tier per ``intake_schemas.TierId``.
TIER: TierId = 1

#: Pinned PaddleOCR-VL checkpoint. Record-only — the actual checkpoint is
#: bundled with the ``paddleocr`` package version, so version-bumping the
#: package is what moves the pin.
PADDLEOCR_VL_VERSION = "PaddleOCR-VL-1.5"

#: PaddlePaddle device string. ``gpu:1`` is the RTX 4060 Ti per the locked
#: device split (GPU 0 reserved for Tier 2 + Tier 3 Qwen).
PADDLE_DEVICE = "gpu:1"

#: Pipeline version stamped onto stub ``FormMetadata``. Phase 5's orchestrator
#: overrides this with the live pipeline version; the stub keeps the form
#: instantiable since FormMetadata.pipeline_version is required.
PIPELINE_VERSION = f"tier1-paddleocr-vl-1.5@{TIER}"

#: Minimum alias-match score (0.0-1.0) required to accept a block → field
#: mapping. Below this, the block is dropped (the field stays unattempted:
#: ``tier_used=None``). 0.3 lets clean "Label: value" blocks pass (e.g.
#: "First Name: Jane" matches alias "First Name" at len(alias)/len(text) ≈
#: 0.55) while rejecting blocks where the alias is a tiny incidental
#: substring of a longer phrase. Phase 6 eval-sweep tunes if needed.
ALIAS_MATCH_THRESHOLD = 0.3

#: Separators stripped between a recognized label and its value text. Matches
#: one leading run of ``:``, ``-``, en-dash (U+2013), em-dash (U+2014), or
#: whitespace at the start of the post-label remainder. ``re.sub(..., count=1)``
#: applied to the remainder slice so a value like "2024-01-15" doesn't get its
#: internal dashes mangled.
_LABEL_VALUE_SEPARATORS = re.compile("^[\\s:\\-\u2013\u2014\t]+")

#: ``alias_table_seed.json`` path. Resolved relative to this file so the
#: import works regardless of the caller's cwd. Repo layout puts the seed
#: at repo-root; this module is at ``cascade/providers/``.
ALIAS_TABLE_PATH = Path(__file__).resolve().parent.parent.parent / "alias_table_seed.json"

#: Map from form class name → set of seed verticals to draw aliases from.
#: Each form class pulls aliases from the union of these verticals' seed
#: records. ``base`` always included for cross-vertical fields (first_name,
#: address_*, etc.). BusinessDocumentForm has no seed records (DocILE's
#: KILE taxonomy isn't in the seed), so it falls through to the synthetic-
#: alias-from-canonical-name path in ``_alias_map_for_form``.
_FORM_VERTICAL_MAP: dict[str, frozenset[str]] = {
    "HealthcareIntakeForm": frozenset({"base", "healthcare"}),
    "InsuranceIntakeForm": frozenset({"base", "insurance"}),
    "HRIntakeForm": frozenset({"base", "hr"}),
    "BusinessDocumentForm": frozenset({"base"}),
}


@lru_cache(maxsize=1)
def _load_alias_table_raw() -> list[dict[str, Any]]:
    """Load + return the ``fields`` array from ``alias_table_seed.json``.

    Cached for the lifetime of the process — the seed file is checked in
    and immutable at runtime. Tests that need a different seed
    ``monkeypatch`` ``ALIAS_TABLE_PATH`` and call ``_load_alias_table_raw.cache_clear()``.
    """
    return json.loads(ALIAS_TABLE_PATH.read_text(encoding="utf-8"))["fields"]


def _alias_map_for_form(form_cls: type[T]) -> dict[str, list[str]]:
    """Build ``{canonical_name: [aliases]}`` for ``form_cls``'s vertical.

    Aliases come from the union of seed records whose ``vertical`` is in
    ``_FORM_VERTICAL_MAP[form_cls.__name__]`` AND whose ``canonical_name`` is a
    field on the form. Order: first-seen wins (base records typically come
    before vertical-specific records in the seed JSON), but duplicates are
    suppressed.

    For canonical_names on the form that have no seed record (e.g., all of
    BusinessDocumentForm's DocILE-derived fields), synthesize a single alias
    from the canonical_name itself (``vendor_name`` → ``Vendor Name``). Crude
    but functional for unit tests; real-world DocILE extraction quality lives
    at Phase 4 PR (c)'s Tier 2 Qwen 7B prompted extraction, not at Tier 1.
    """
    canonical_in_form = set(get_field_metadata(form_cls).keys())
    vertical_filter = _FORM_VERTICAL_MAP.get(form_cls.__name__, frozenset({"base"}))
    raw = _load_alias_table_raw()

    result: dict[str, list[str]] = {}
    for record in raw:
        if record["vertical"] not in vertical_filter:
            continue
        name = record["canonical_name"]
        if name not in canonical_in_form:
            continue
        bucket = result.setdefault(name, [])
        for alias in record["aliases"]:
            if alias not in bucket:
                bucket.append(alias)

    # Fallback: synthesize an alias from the canonical_name for any field
    # not covered by the seed.
    for name in canonical_in_form:
        if name not in result:
            result[name] = [name.replace("_", " ").title()]

    return result


def _strip_label_prefix(text: str, alias: str) -> str | None:
    """Strip ``alias`` (case-insensitive) + a separator from ``text``'s start-or-mid.

    Returns the remaining value text, or None if the alias isn't found or the
    remainder is empty. The match finds the FIRST occurrence of the alias
    anywhere in ``text`` — block_content may include some preamble before the
    label, but typically the alias appears at the start.
    """
    if not alias.strip():
        return None
    upper_text = text.upper()
    upper_alias = alias.upper().strip()
    pos = upper_text.find(upper_alias)
    if pos < 0:
        return None
    remainder = text[pos + len(upper_alias) :]
    remainder = _LABEL_VALUE_SEPARATORS.sub("", remainder, count=1).rstrip()
    return remainder if remainder else None


def _match_block(
    text: str, alias_map: dict[str, list[str]]
) -> tuple[str, str | None, float] | None:
    """Match a block's text against the alias table.

    Returns ``(canonical_name, value_or_None, score)`` for the best match, or
    None if no match clears ``ALIAS_MATCH_THRESHOLD``.

    Scoring: ``len(alias) / len(text)`` clamped to [0, 1]. Longer alias
    relative to block text = higher confidence. A label-only block (text
    equals the alias) scores 1.0 but ``value`` will be None — Phase 5's
    orchestrator can pair label-only blocks with their spatially-adjacent
    value blocks; V1's single-block heuristic skips them.

    Tie-breaking on equal scores: first-seen wins. Aliases are iterated in
    seed order, so position 0 (the canonical phrasing) wins over later
    variants of the same field.
    """
    text_stripped = text.strip()
    if not text_stripped:
        return None

    text_len = len(text_stripped)
    best: tuple[str, str | None, float] | None = None

    for canonical_name, aliases in alias_map.items():
        for alias in aliases:
            alias_stripped = alias.strip()
            if not alias_stripped:
                continue
            if alias_stripped.upper() not in text_stripped.upper():
                continue
            score = min(len(alias_stripped) / text_len, 1.0)
            if best is None or score > best[2]:
                value = _strip_label_prefix(text_stripped, alias_stripped)
                best = (canonical_name, value, score)

    if best is not None and best[2] >= ALIAS_MATCH_THRESHOLD:
        return best
    return None


def _parse_bbox(bbox: Any, page_size_px: tuple[int, int] | None = None) -> BoundingBox | None:
    """Parse a 4-tuple bbox, normalizing pixel coords to [0, 1] when possible.

    Accepts ``[x1, y1, x2, y2]`` as list/tuple of numerics. PaddleOCR-VL's
    real output is pixel coords; the stub fixtures in this repo use
    normalized [0, 1]. The pixel-vs-normalized detection is purely
    value-magnitude — any coordinate > 1.0 implies pixel coords.

    Returns ``BoundingBox(page_number=1, ...)`` (single-page assumption;
    Phase 5's orchestrator rewrites page_number when stitching multi-page
    extractions). Returns None on malformed input or when pixel coords are
    given without page_size_px.
    """
    if not isinstance(bbox, list | tuple) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None

    if max(x1, y1, x2, y2) > 1.0:
        # Pixel coords. Need page_size_px to normalize.
        if page_size_px is None:
            return None
        w, h = page_size_px
        if w <= 0 or h <= 0:
            return None
        x1, x2 = x1 / w, x2 / w
        y1, y2 = y1 / h, y2 / h

    # Clamp to [0, 1] defensively — a pixel bbox that slightly exceeds the
    # page (e.g. a glyph the layout detector traced past the page edge)
    # shouldn't reject the whole match.
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))

    try:
        return BoundingBox(page_number=1, x1=x1, y1=y1, x2=x2, y2=y2)
    except ValueError:
        # Defensive: any future BoundingBox validator addition that rejects
        # malformed input shouldn't crash the cascade. As of intake_schemas
        # today, BoundingBox accepts any 4-float quadruple — this branch is
        # dead code currently but cheap forward-compat.
        return None


def _parse_response(raw: dict[str, Any], form_cls: type[T]) -> T:
    """Parse a PaddleOCR-VL response into a populated ``form_cls`` instance.

    Pure function — no I/O, no model state. The cached-replay path and the
    live path both run through this so behavior stays identical.

    Expected response shape (the layout-parser output PaddleOCR-VL actually
    returns)::

        {
            "parsing_res_list": [
                {
                    "block_bbox": [x1, y1, x2, y2],
                    "block_label": "text",
                    "block_content": "Patient Name: Jane Doe",
                },
                ...
            ],
            "page_size_px": [w, h],  # optional; needed only if bboxes are pixel
        }

    The alias-table-driven post-processor:
      1. Builds the alias map for ``form_cls``'s vertical.
      2. Walks ``parsing_res_list``.
      3. For each block, finds the best-matching canonical field.
      4. Extracts the value text by stripping the recognized label prefix.
      5. Populates the form, taking the highest-scoring match per field.

    Locked rules:
      - ``tier_used=1`` stamped on every populated field. Confidently-blank
        fields (model attempted, value=None) aren't producible from
        PaddleOCR-VL's layout-parser output, so V1 Tier 1 never emits them —
        all populated fields carry a non-None value.
      - Unknown ``block_label`` types (table, formula, etc.) don't fail —
        the post-processor only reads ``block_content``, so non-text labels
        with text content still get matched.
      - Confidence is the alias-match score from ``_match_block``, clamped
        to [0, 1]. NOT a model-reported confidence (PaddleOCR-VL doesn't
        emit per-field confidences).
    """
    blocks = raw.get("parsing_res_list", [])
    if not isinstance(blocks, list):
        return form_cls(metadata=_stub_metadata(form_cls))

    page_size = _parse_page_size(raw.get("page_size_px"))
    alias_map = _alias_map_for_form(form_cls)

    # canonical_name → (ExtractedField, score). Highest score wins on collision.
    best_by_field: dict[str, tuple[ExtractedField[Any], float]] = {}

    for block in blocks:
        if not isinstance(block, dict):
            continue
        content = block.get("block_content")
        if not isinstance(content, str) or not content.strip():
            continue

        match = _match_block(content, alias_map)
        if match is None:
            continue
        canonical_name, value, score = match
        if value is None:
            # Label-only block; Phase 5 orchestrator can pair labels with
            # adjacent value blocks via layout heuristics. V1 Tier 1 skips.
            continue

        bbox = _parse_bbox(block.get("block_bbox"), page_size)
        field = ExtractedField(
            value=value,
            confidence=score,
            tier_used=TIER,
            raw_text=content,
            bounding_box=bbox,
        )
        existing = best_by_field.get(canonical_name)
        if existing is None or score > existing[1]:
            best_by_field[canonical_name] = (field, score)

    field_overrides = {name: ef for name, (ef, _score) in best_by_field.items()}
    return form_cls(metadata=_stub_metadata(form_cls), **field_overrides)


def _parse_page_size(raw: Any) -> tuple[int, int] | None:
    """Parse ``[w, h]`` from a raw_response field. Returns None on malformed input."""
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        return None
    try:
        w, h = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (w, h)


def _stub_metadata(form_cls: type) -> FormMetadata:
    """Build a placeholder ``FormMetadata`` for a single provider call.

    The provider doesn't know the upstream ``source_document_id``; Phase 5's
    orchestrator replaces this with the real provenance value when assembling
    the cascade output. The stub just keeps the Pydantic instance valid.
    """
    return FormMetadata(
        form_type=form_cls.__name__,
        source_document_id="<pending-orchestrator>",
        extraction_timestamp=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )


def _load_paddleocr_vl_pipeline() -> Any:
    """Lazy paddle + paddleocr import; pin device; instantiate pipeline.

    Called once per provider instance (memoized in ``Tier1PaddleOcrLocal``).
    Raises ``ImportError`` with an install hint when paddle/paddleocr are
    missing — the cached-replay path never reaches this code, so CI doesn't
    need the GPU stack.

    Tests stub via ``monkeypatch.setattr`` on this module attribute.
    """
    try:
        import paddle  # type: ignore[import-not-found]
        from paddleocr import PaddleOCRVL  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "Tier 1 live inference requires paddlepaddle-gpu + paddleocr. "
            "See docs/local-development.md `Tier 1 PaddleOCR-VL setup` for "
            "the install commands. Cached-replay tests do not require this "
            "stack."
        ) from e

    paddle.set_device(PADDLE_DEVICE)
    return PaddleOCRVL()


def _invoke_pipeline(pipeline: Any, png: bytes) -> dict[str, Any]:
    """Run one PaddleOCR-VL prediction. Tests stub via ``monkeypatch.setattr``.

    No ``prompt`` parameter — PaddleOCR-VL is a layout parser, not a prompted
    VL model. ``pipeline.predict(input=img)`` returns ``parsing_res_list``
    (verbatim from upstream); ``page_size_px`` is captured from the PIL
    image so ``_parse_response`` can normalize pixel bboxes to [0, 1].

    PaddleOCR-VL's ``predict`` returns different shapes across versions; we
    normalize to a single ``dict`` containing ``parsing_res_list`` + the
    PIL-captured ``page_size_px``.
    """
    img = Image.open(io.BytesIO(png))
    page_size_px = list(img.size)  # PIL .size is (w, h)
    result = pipeline.predict(input=img)

    if isinstance(result, dict):
        normalized: dict[str, Any] = dict(result)
    elif isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        normalized = dict(result[0])
    else:
        normalized = {"_warning": "unexpected_predict_shape", "raw": repr(result)}

    # page_size_px is the parser's bridge from pixel-bbox land to normalized
    # [0, 1] land. Keep it alongside parsing_res_list in the raw_response so
    # the cached fixture round-trips correctly without re-loading the PNG.
    normalized.setdefault("page_size_px", page_size_px)
    return normalized


class Tier1PaddleOcrLocal:
    """Conforms to ``cascade.providers._base.CascadeProvider``.

    The class is a thin shell: state is just the lazily-loaded pipeline. All
    parsing lives in module-level pure functions so the cached-replay path
    and the live path share one parser.
    """

    name: str = PROVIDER_NAME
    tier: TierId = TIER

    def __init__(self) -> None:
        self._pipeline: Any | None = None

    def _ensure_pipeline_loaded(self) -> Any:
        """Lazy-load the PaddleOCR-VL pipeline. Memoized."""
        if self._pipeline is None:
            self._pipeline = _load_paddleocr_vl_pipeline()
        return self._pipeline

    def extract(self, png: bytes, form_cls: type[T]) -> ProviderResult[T]:
        """Extract fields from a single-page PNG.

        Cache-first: if ``EVAL_LIVE`` is unset and a cached response exists
        for ``sha256(png)``, parse the cached payload and return with
        ``latency_ms=0.0``. Otherwise call live, persist the response, and
        return real telemetry.

        ``image_sha256`` is recomputed from ``png`` here (not pulled from a
        sidecar) so a caller bug pairing the wrong PNG with a stale hash
        surfaces as a cache miss rather than serving the wrong cached result.
        """
        image_sha256 = hashlib.sha256(png).hexdigest()

        if not eval_cache.is_live_mode():
            cached = eval_cache.load_cached(PROVIDER_NAME, image_sha256)
            if cached is not None:
                return ProviderResult(
                    form=_parse_response(cached, form_cls),
                    latency_ms=0.0,
                    cost_usd=0.0,
                    raw_response=cached,
                )

        pipeline = self._ensure_pipeline_loaded()
        t0 = time.perf_counter()
        raw_response = _invoke_pipeline(pipeline, png)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        eval_cache.save_cached(PROVIDER_NAME, image_sha256, raw_response)
        return ProviderResult(
            form=_parse_response(raw_response, form_cls),
            latency_ms=latency_ms,
            cost_usd=0.0,
            raw_response=raw_response,
        )
