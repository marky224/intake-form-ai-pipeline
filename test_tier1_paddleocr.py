"""Tests for ``cascade.providers.tier1_paddleocr_local``.

Three layers:

1. **Pure parser** — ``_parse_response`` + ``_parse_bbox`` + ``_build_prompt``.
   No pipeline, no I/O. Fast.
2. **Cached replay** — ``extract()`` short-circuits to cache hits when
   ``EVAL_LIVE`` is unset. The Phase-6 eval-cache regen workflow flips
   ``EVAL_LIVE=true`` to refresh fixtures.
3. **Live path** — gated by ``@pytest.mark.slow``. Exercises the
   live-inference branch with a stubbed pipeline so the live code path
   has a test without requiring the GPU stack in CI.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from cascade import eval_cache
from cascade.providers import tier1_paddleocr_local
from cascade.providers._base import CascadeProvider, ProviderResult
from cascade.providers.tier1_paddleocr_local import (
    PADDLEOCR_VL_VERSION,
    PROVIDER_NAME,
    TIER,
    Tier1PaddleOcrLocal,
    _build_prompt,
    _parse_bbox,
    _parse_response,
)
from intake_schemas import (
    BusinessDocumentForm,
    DataClass,
    HealthcareIntakeForm,
    get_field_metadata,
)

# Tiny PNG-shaped byte payload — sha256 is stable across runs so tests can pin
# against it. Real provider runs use renderer output, not this stub.
_FAKE_PNG = b"PNG_PAYLOAD_for_tests"
_FAKE_PNG_SHA256 = hashlib.sha256(_FAKE_PNG).hexdigest()


@pytest.fixture
def isolated_cache_root(tmp_path, monkeypatch):
    """Swap CACHE_ROOT to a tmp dir so tests don't write to checked-in fixtures."""
    monkeypatch.setattr(eval_cache, "CACHE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def eval_live_off(monkeypatch):
    """Force EVAL_LIVE off — exercises the cache-first path."""
    monkeypatch.delenv("EVAL_LIVE", raising=False)


@pytest.fixture
def eval_live_on(monkeypatch):
    """Force EVAL_LIVE on — bypasses cache, hits the live path."""
    monkeypatch.setenv("EVAL_LIVE", "true")


# ---------------------------------------------------------------------------
# Provider metadata + Protocol conformance
# ---------------------------------------------------------------------------


def test_provider_metadata_constants():
    """name + tier are stable; PaddleOCR-VL version pin is recorded."""
    assert PROVIDER_NAME == "tier1_paddleocr_local"
    assert TIER == 1
    assert PADDLEOCR_VL_VERSION == "PaddleOCR-VL-1.5"


def test_provider_satisfies_cascade_provider_protocol():
    """Tier1PaddleOcrLocal conforms to the locked Protocol shape."""
    assert isinstance(Tier1PaddleOcrLocal(), CascadeProvider)


def test_provider_attributes_match_protocol():
    p = Tier1PaddleOcrLocal()
    assert p.name == PROVIDER_NAME
    assert p.tier == TIER


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_includes_every_canonical_field():
    """Prompt enumerates each form_cls field so the model knows what to extract."""
    prompt = _build_prompt(BusinessDocumentForm)
    field_names = set(get_field_metadata(BusinessDocumentForm).keys())
    for name in field_names:
        assert f"- {name}:" in prompt, f"prompt missing field {name!r}"


def test_build_prompt_includes_field_descriptions():
    """Each field's description is included so the model has semantic context."""
    prompt = _build_prompt(BusinessDocumentForm)
    meta = get_field_metadata(BusinessDocumentForm)
    for description in (meta["vendor_name"].description, meta["iban"].description):
        assert description in prompt


def test_build_prompt_works_for_healthcare_form():
    """Same prompt template renders cleanly for the other vertical."""
    prompt = _build_prompt(HealthcareIntakeForm)
    assert "first_name:" in prompt
    assert "insurance_member_id:" in prompt


# ---------------------------------------------------------------------------
# _parse_bbox
# ---------------------------------------------------------------------------


def test_parse_bbox_happy_path():
    bb = _parse_bbox({"bbox": [0.1, 0.2, 0.3, 0.4]})
    assert bb is not None
    assert bb.page_number == 1
    assert bb.x1 == 0.1
    assert bb.x2 == 0.3


def test_parse_bbox_absent_returns_none():
    assert _parse_bbox({"value": "x"}) is None


def test_parse_bbox_wrong_length_returns_none():
    assert _parse_bbox({"bbox": [0.1, 0.2, 0.3]}) is None
    assert _parse_bbox({"bbox": [0.1, 0.2, 0.3, 0.4, 0.5]}) is None


def test_parse_bbox_non_numeric_returns_none():
    assert _parse_bbox({"bbox": ["a", "b", "c", "d"]}) is None


def test_parse_bbox_accepts_int_coords():
    bb = _parse_bbox({"bbox": [1, 2, 3, 4]})
    assert bb is not None
    assert bb.x1 == 1.0


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


def test_parse_response_populates_named_fields():
    raw = {
        "fields": [
            {"name": "vendor_name", "value": "ACME", "confidence": 0.95},
            {"name": "iban", "value": "DE89370400440532013000", "confidence": 0.99},
        ]
    }
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.value == "ACME"
    assert form.iban.value == "DE89370400440532013000"


def test_parse_response_stamps_tier_used_on_populated_fields():
    """Every field the model touches gets tier_used=1."""
    raw = {"fields": [{"name": "vendor_name", "value": "ACME", "confidence": 0.95}]}
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.tier_used == 1


def test_parse_response_stamps_tier_used_on_confidently_blank_fields():
    """value=null + tier_used=1 = attempted, returned blank (vs unattempted)."""
    raw = {"fields": [{"name": "vendor_email", "value": None, "confidence": 0.92}]}
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_email.value is None
    assert form.vendor_email.tier_used == 1
    assert form.vendor_email.confidence == 0.92


def test_parse_response_leaves_unmentioned_fields_unattempted():
    """Fields not in the response stay at default — tier_used=None."""
    raw = {"fields": [{"name": "vendor_name", "value": "ACME", "confidence": 0.95}]}
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.iban.tier_used is None
    assert form.iban.value is None


def test_parse_response_silently_drops_unknown_field_names():
    """A hallucinated 'foo_bar' shouldn't break the rest of the extraction."""
    raw = {
        "fields": [
            {"name": "vendor_name", "value": "ACME", "confidence": 0.95},
            {"name": "totally_made_up_field", "value": "junk", "confidence": 0.99},
        ]
    }
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.value == "ACME"
    assert not hasattr(form, "totally_made_up_field")


def test_parse_response_clamps_high_confidence():
    """Pydantic's [0, 1] validator would reject 1.5; clamp first."""
    raw = {"fields": [{"name": "vendor_name", "value": "x", "confidence": 1.5}]}
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.confidence == 1.0


def test_parse_response_clamps_negative_confidence():
    raw = {"fields": [{"name": "vendor_name", "value": "x", "confidence": -0.3}]}
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.confidence == 0.0


def test_parse_response_handles_non_numeric_confidence():
    raw = {"fields": [{"name": "vendor_name", "value": "x", "confidence": "high"}]}
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.confidence == 0.0


def test_parse_response_attaches_bounding_box():
    raw = {
        "fields": [
            {
                "name": "amount_total_gross",
                "value": "1,234.56",
                "confidence": 0.95,
                "bbox": [0.81, 0.83, 0.88, 0.85],
            }
        ]
    }
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.amount_total_gross.bounding_box is not None
    assert form.amount_total_gross.bounding_box.page_number == 1
    assert form.amount_total_gross.bounding_box.x1 == 0.81


def test_parse_response_captures_raw_text_when_present():
    raw = {
        "fields": [
            {
                "name": "vendor_name",
                "value": "ACME Co.",
                "confidence": 0.95,
                "raw_text": "AGME Co.",  # OCR variant
            }
        ]
    }
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.raw_text == "AGME Co."


def test_parse_response_handles_empty_fields_list():
    """Model returned nothing — form valid, all fields unattempted."""
    form = _parse_response({"fields": []}, BusinessDocumentForm)
    assert form.vendor_name.tier_used is None
    assert form.metadata.form_type == "BusinessDocumentForm"


def test_parse_response_handles_missing_fields_key():
    """Malformed response — be lenient, return empty extraction."""
    form = _parse_response({}, BusinessDocumentForm)
    assert form.vendor_name.tier_used is None


def test_parse_response_skips_non_dict_entries():
    raw = {
        "fields": ["not a dict", None, {"name": "vendor_name", "value": "ok", "confidence": 0.9}]
    }
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.value == "ok"


def test_parse_response_works_for_healthcare_form():
    """Same parser handles both verticals."""
    raw = {
        "fields": [
            {"name": "first_name", "value": "Jane", "confidence": 0.96},
            {"name": "patient_id", "value": "MRN-12345", "confidence": 0.89},
        ]
    }
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Jane"
    assert form.patient_id.value == "MRN-12345"
    # HealthcareIntakeForm elevates first_name to PHI
    assert get_field_metadata(HealthcareIntakeForm)["first_name"].data_class == DataClass.PHI


# ---------------------------------------------------------------------------
# extract() — cache-first replay path
# ---------------------------------------------------------------------------


def test_extract_returns_cached_response_when_cache_hit(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache hit with EVAL_LIVE unset → no live call, latency_ms=0.0."""
    # Seed the cache with a known response keyed on the fake PNG's sha256.
    eval_cache.save_cached(
        PROVIDER_NAME,
        _FAKE_PNG_SHA256,
        {
            "fields": [
                {"name": "vendor_name", "value": "CachedCorp", "confidence": 0.92},
            ]
        },
    )

    # Sentinel: live path must NOT be reached. Replace the pipeline-loading
    # helper with a raiser so a cache miss would visibly fail.
    def _should_not_be_called() -> None:
        raise AssertionError("live path called despite cache hit")

    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", _should_not_be_called)

    provider = Tier1PaddleOcrLocal()
    result = provider.extract(_FAKE_PNG, BusinessDocumentForm)

    assert isinstance(result, ProviderResult)
    assert result.form.vendor_name.value == "CachedCorp"
    assert result.form.vendor_name.tier_used == 1
    assert result.latency_ms == 0.0
    assert result.cost_usd == 0.0
    assert result.raw_response["fields"][0]["name"] == "vendor_name"


def test_extract_falls_through_to_live_on_cache_miss(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache miss (no EVAL_LIVE) → still hits live path per starter-prompt spec."""
    stub_response = {
        "fields": [{"name": "iban", "value": "DE89370400440532013000", "confidence": 0.99}]
    }
    monkeypatch.setattr(
        tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub-pipeline"
    )
    monkeypatch.setattr(
        tier1_paddleocr_local,
        "_invoke_pipeline",
        lambda pipeline, png, prompt: stub_response,
    )

    provider = Tier1PaddleOcrLocal()
    result = provider.extract(_FAKE_PNG, BusinessDocumentForm)

    assert result.form.iban.value == "DE89370400440532013000"
    assert result.latency_ms >= 0.0  # live path → real wall-clock
    assert result.raw_response == stub_response


def test_extract_writes_back_to_cache_after_live_call(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Live call success → response persisted under the PNG's sha256."""
    stub_response = {"fields": [{"name": "vendor_name", "value": "Fresh", "confidence": 0.88}]}
    monkeypatch.setattr(
        tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub-pipeline"
    )
    monkeypatch.setattr(
        tier1_paddleocr_local,
        "_invoke_pipeline",
        lambda pipeline, png, prompt: stub_response,
    )

    Tier1PaddleOcrLocal().extract(_FAKE_PNG, BusinessDocumentForm)

    # Second call should hit cache and skip live.
    monkeypatch.setattr(
        tier1_paddleocr_local,
        "_invoke_pipeline",
        lambda *args, **kw: pytest.fail("second call should be cache hit"),
    )
    result2 = Tier1PaddleOcrLocal().extract(_FAKE_PNG, BusinessDocumentForm)
    assert result2.form.vendor_name.value == "Fresh"
    assert result2.latency_ms == 0.0


def test_extract_bypasses_cache_when_eval_live_set(isolated_cache_root, eval_live_on, monkeypatch):
    """EVAL_LIVE=true → always call live, overwriting any cached response."""
    eval_cache.save_cached(
        PROVIDER_NAME,
        _FAKE_PNG_SHA256,
        {"fields": [{"name": "vendor_name", "value": "Stale", "confidence": 0.5}]},
    )
    fresh = {"fields": [{"name": "vendor_name", "value": "Fresh", "confidence": 0.99}]}
    monkeypatch.setattr(
        tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub-pipeline"
    )
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", lambda *a, **k: fresh)

    result = Tier1PaddleOcrLocal().extract(_FAKE_PNG, BusinessDocumentForm)
    assert result.form.vendor_name.value == "Fresh"
    # Cache now holds the fresh response — verify by reading directly.
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == fresh


def test_extract_recomputes_sha256_from_png_bytes(isolated_cache_root, eval_live_off, monkeypatch):
    """Provider keys the cache on sha256(png) not on any caller-supplied hash."""
    # Seed cache under the CORRECT sha for _FAKE_PNG.
    fresh = {"fields": [{"name": "vendor_name", "value": "Correct", "confidence": 0.9}]}
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, fresh)
    # A DIFFERENT PNG (different sha) should miss cache and hit live, even
    # though both PNGs would be paired with the same sidecar in a buggy call.
    other_png = b"DIFFERENT_PNG_BYTES"
    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub")
    live_response = {"fields": [{"name": "vendor_name", "value": "FromLive", "confidence": 0.8}]}
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", lambda *a, **k: live_response)
    result = Tier1PaddleOcrLocal().extract(other_png, BusinessDocumentForm)
    assert result.form.vendor_name.value == "FromLive"


def test_extract_caches_under_correct_sha_after_live_call(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Live response is stored under sha256(png), not under a stale hash."""
    payload = {"fields": [{"name": "vendor_name", "value": "X", "confidence": 0.8}]}
    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub")
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", lambda *a, **k: payload)
    Tier1PaddleOcrLocal().extract(_FAKE_PNG, BusinessDocumentForm)
    # The cache file must exist under the canonical sha256(_FAKE_PNG).
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == payload


# ---------------------------------------------------------------------------
# Live path tests (gated)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_live_inference_smoke(isolated_cache_root, eval_live_on):
    """Skipped unless paddle is installed AND EVAL_LIVE=true.

    Real live-inference validation runs as the deliverable step for this PR
    against the 10-doc validation set (``tests/fixtures/eval-validation/``).
    This smoke test just confirms the live path doesn't blow up on import
    when paddle IS available. CI never sets EVAL_LIVE so this stays skipped.
    """
    try:
        import paddle  # noqa: F401
    except ImportError:
        pytest.skip("paddle not installed; see docs/local-development.md Tier 1 setup")

    # If we got here, paddle is available; instantiating the provider should
    # not fail at construction time (pipeline loads lazily).
    provider = Tier1PaddleOcrLocal()
    assert provider.name == PROVIDER_NAME


def test_load_paddleocr_vl_pipeline_raises_helpful_error_when_paddle_missing(monkeypatch):
    """Without paddle installed the live path raises ImportError with an install hint."""
    if "paddle" in os.environ.get("_PYTHON_INSTALLED_PACKAGES", ""):
        pytest.skip("paddle is installed; can't exercise the missing-import path here")
    # Force ImportError by removing paddle from sys.modules and shadowing the import.
    import sys

    monkeypatch.setitem(sys.modules, "paddle", None)
    with pytest.raises(ImportError, match="paddlepaddle-gpu"):
        tier1_paddleocr_local._load_paddleocr_vl_pipeline()


# ---------------------------------------------------------------------------
# Validation set: end-to-end cached replay against the 6 CMS-1500 docs
# ---------------------------------------------------------------------------

# Note on corpus composition: the starter prompt called for 10 docs (5
# CMS-1500 + 5 DocILE pages). Locked 2026-05-13 to CMS-1500-only — DocILE
# pages cannot be redistributed in this MIT public repo because DocILE is
# CC-BY-NC-ND 4.0. DocILE-side validation is a local-only workflow on
# Mark's GPU machine. See tests/fixtures/eval-cache/README.md.

import pathlib  # noqa: E402

VALIDATION_DIR = pathlib.Path("tests/fixtures/eval-validation/cms1500")


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


def test_validation_corpus_present():
    """The checked-in CMS-1500 validation set exists and has 6 docs."""
    pngs = _validation_pngs()
    assert len(pngs) == 6, f"Expected 6 validation PNGs, found {len(pngs)}"


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name)
def test_tier1_cached_replay_on_validation_doc(png_path, monkeypatch):
    """Each validation doc has a cached Tier 1 response that parses cleanly.

    Exercises the full pipeline: real PNG bytes -> sha256 -> cache lookup ->
    _parse_response -> populated HealthcareIntakeForm. The acceptance gate
    per the PR (a+b) starter: 'Tier 1 must return SOMETHING (parseable
    response, no exceptions) on all N docs.'
    """
    monkeypatch.delenv("EVAL_LIVE", raising=False)

    # Sentinel: cache must HIT — if it misses the live path is hit and the
    # test fails loudly rather than silently re-running inference.
    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            f"Cache miss on {png_path.name}: cached fixture missing or PNG bytes drifted."
        )

    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", _should_not_be_called)
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", _should_not_be_called)

    provider = Tier1PaddleOcrLocal()
    result = provider.extract(png_path.read_bytes(), HealthcareIntakeForm)

    # Acceptance: parseable response with no exceptions.
    assert isinstance(result, ProviderResult)
    assert result.latency_ms == 0.0  # cache hit
    assert result.cost_usd == 0.0
    # At least one field should be populated (the stub seeds always include
    # demographic fields; live PaddleOCR-VL responses will too).
    populated = [
        name
        for name in get_field_metadata(HealthcareIntakeForm)
        if getattr(result.form, name).tier_used == 1
    ]
    assert populated, f"{png_path.name}: no fields populated in cached response"
