"""Tier 1 PaddleOCR-VL local provider.

Pinned model: **PaddleOCR-VL-1.5** (released 2026-01-29; OmniDocBench v1.5 =
94.5%; irregular-shaped bbox localization; seal recognition; robust to scan
skew/warping/screen-photography/complex illumination). Pulled via the
``paddleocr`` Python package — bumping the package version is what pins the
checkpoint, since ``PaddleOCRVL()`` constructs from the package's bundled
weights.

**Device pin:** ``gpu:1`` (RTX 4060 Ti, 16 GB). GPU 0 (RTX 4080, 16 GB) is
reserved for Tier 3a Qwen 2.5 VL 32B so the cascade can hold both models
resident across a batch without paging.

**Role in the cascade:** cheap-and-fast first-pass extraction, sub-second per
page, high precision on the easy 60-70% of fields. Lower-confidence fields
escalate to Tier 2 (Textract) per the Phase 5 orchestrator's confidence
threshold (0.85 — locked, tuned in Phase 6).

**Cost:** $0.00/call (local inference).

**Lazy imports:** ``paddle`` and ``paddleocr`` are imported inside
``_load_paddleocr_vl_pipeline`` rather than at module top-level so the
``cascade`` package loads cleanly on machines without the GPU stack (e.g.,
CI). Cached-replay tests never touch the live path; only the
``EVAL_LIVE=true`` workflow on the build machine pays the import + load cost.
"""

from __future__ import annotations

import hashlib
import io
import time
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from cascade import eval_cache
from cascade.providers._base import ProviderResult, T
from intake_schemas import (
    BoundingBox,
    ExtractedField,
    FieldMeta,
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
#: device split (GPU 0 reserved for Tier 3a Qwen).
PADDLE_DEVICE = "gpu:1"

#: Pipeline version stamped onto stub ``FormMetadata``. Phase 5's orchestrator
#: overrides this with the live pipeline version; the stub keeps the form
#: instantiable since FormMetadata.pipeline_version is required.
PIPELINE_VERSION = f"tier1-paddleocr-vl-1.5@{TIER}"

PROMPT_TEMPLATE = """\
Extract the following fields from this document. For each field, return the
raw text as it appears, a confidence in [0, 1], and a bounding box in
normalized [0, 1] coordinates (top-left origin). If a field is not present,
return value=null. Respond in JSON with a single top-level key "fields"
containing a list of objects with keys: name, value, confidence, bbox.

Fields to extract:
{field_descriptions}
"""


def _build_prompt(form_cls: type[T]) -> str:
    """Generate the structured-output prompt from ``form_cls`` FieldMeta."""
    metadata: dict[str, FieldMeta] = get_field_metadata(form_cls)
    lines = [f"- {name}: {meta.description}" for name, meta in metadata.items()]
    return PROMPT_TEMPLATE.format(field_descriptions="\n".join(lines))


def _parse_bbox(entry: dict[str, Any]) -> BoundingBox | None:
    """Parse a normalized bbox quadruple. Return None on absent/malformed input.

    PaddleOCR-VL emits bboxes in normalized [0, 1] coordinates with top-left
    origin — same convention as DocILE annotations + the schema's BoundingBox.
    Single-page assumption: provider operates page-at-a-time, so
    ``page_number=1`` is correct for every bbox we emit. Phase 5's orchestrator
    rewrites bbox.page_number when stitching multi-page extractions.
    """
    bbox = entry.get("bbox")
    if not isinstance(bbox, list | tuple) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    return BoundingBox(page_number=1, x1=x1, y1=y1, x2=x2, y2=y2)


def _parse_response(raw: dict[str, Any], form_cls: type[T]) -> T:
    """Parse a raw PaddleOCR-VL response into a populated ``form_cls`` instance.

    Pure function — no I/O, no model state. The cached-replay path and the
    live path both run through this so behavior stays identical.

    Expected response shape::

        {
            "fields": [
                {
                    "name": "vendor_name",
                    "value": "ACME Industrial Supply Co.",
                    "confidence": 0.95,
                    "bbox": [0.65, 0.24, 0.84, 0.25],
                    "raw_text": "ACME Industrial Supply Co."  # optional
                },
                {"name": "iban", "value": null, "confidence": 0.99, "bbox": null},
                ...
            ]
        }

    Locked rules:
      - ``tier_used`` is stamped on every field the model touched (including
        confidently-blank ``value=null`` entries). Fields not in the response
        stay at default — ``tier_used=None`` distinguishes "never attempted"
        from "attempted, blank" per ``compute_form_confidence`` semantics.
      - Unknown field names (model hallucinated outside ``form_cls``'s
        canonical set) are silently dropped. A bad-actor entry shouldn't
        break the rest of the extraction.
      - Confidence is clamped to [0, 1]. Pydantic's validator would reject
        out-of-range otherwise.
    """
    valid_names = set(get_field_metadata(form_cls).keys())

    field_overrides: dict[str, ExtractedField[Any]] = {}
    for entry in raw.get("fields", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name not in valid_names:
            continue

        confidence_raw = entry.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        value = entry.get("value")
        raw_text = entry.get("raw_text")
        if not isinstance(raw_text, str):
            raw_text = None
        bbox = _parse_bbox(entry)

        field_overrides[name] = ExtractedField(
            value=value,
            confidence=confidence,
            tier_used=TIER,
            raw_text=raw_text,
            bounding_box=bbox,
        )

    return form_cls(metadata=_stub_metadata(form_cls), **field_overrides)


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
            "Install via `uv sync --extra paddle`. Cached-replay tests do not "
            "require this stack."
        ) from e

    paddle.set_device(PADDLE_DEVICE)
    return PaddleOCRVL()


def _invoke_pipeline(pipeline: Any, png: bytes, prompt: str) -> dict[str, Any]:
    """Run one PaddleOCR-VL prediction. Tests stub via ``monkeypatch.setattr``.

    Wraps the pipeline.predict call so the bytes-to-PIL conversion + response
    normalization live in one place. PaddleOCR-VL's ``predict`` returns
    different shapes across versions; we normalize to a single ``dict``.
    """
    img = Image.open(io.BytesIO(png))
    result = pipeline.predict(input=img, prompt=prompt)
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        return result[0]
    return {"raw": repr(result), "_warning": "unexpected_predict_shape"}


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

        prompt = _build_prompt(form_cls)
        pipeline = self._ensure_pipeline_loaded()
        t0 = time.perf_counter()
        raw_response = _invoke_pipeline(pipeline, png, prompt)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        eval_cache.save_cached(PROVIDER_NAME, image_sha256, raw_response)
        return ProviderResult(
            form=_parse_response(raw_response, form_cls),
            latency_ms=latency_ms,
            cost_usd=0.0,
            raw_response=raw_response,
        )
