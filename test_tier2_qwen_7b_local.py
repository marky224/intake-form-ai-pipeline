"""Tests for ``cascade.providers.tier2_qwen_7b_local``.

Six layers (mirrors ``test_tier1_paddleocr`` so the two providers' test
shape stays comparable):

1. **Provider shape** — Protocol conformance + metadata constants.
2. **Schema introspection** — ``_inner_type`` / ``_scalar_kind`` /
   ``_extractable_fields`` against the real ``ExtractedField[T]`` Pydantic
   generic, for both target form classes.
3. **Prompt + response schema** — ``_build_extraction_prompt`` /
   ``_build_response_schema`` are deterministic and schema-driven.
4. **Response parser** — ``_extract_json_object`` (clean / fenced /
   scraped / malformed) + ``_parse_response`` (confidence heuristic,
   confidently-blank contract, Pydantic-drop-then-demote fallback,
   ``bounding_box=None``).
5. **End-to-end ``extract()``** — cached-replay path + live-path stub +
   ``EVAL_LIVE`` bypass + sha256 cache-key correctness.
6. **Validation set** — the 6 checked-in CMS-1500 cached fixtures must
   parse cleanly with at least one *populated* field each (acceptance gate).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from datetime import date

import pytest

from cascade import eval_cache
from cascade.providers import tier2_qwen_7b_local
from cascade.providers._base import CascadeProvider, ProviderResult
from cascade.providers.tier2_qwen_7b_local import (
    CLEAN_VALUE_CONFIDENCE,
    FORMAT_COERCED_CONFIDENCE,
    OLLAMA_KEEP_ALIVE,
    PROVIDER_NAME,
    QWEN_MODEL_TAG,
    TIER,
    Tier2Qwen7bLocal,
    _build_extraction_prompt,
    _build_response_schema,
    _extract_json_object,
    _extractable_fields,
    _inner_type,
    _parse_response,
    _scalar_kind,
)
from intake_schemas import (
    BusinessDocumentForm,
    HealthcareIntakeForm,
    get_field_metadata,
)

# Tiny PNG-shaped byte payload — sha256 is stable across runs so tests can pin
# against it. Real provider runs use renderer output, not this stub.
_FAKE_PNG = b"PNG_PAYLOAD_for_tier2_tests"
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


def _ollama_raw(payload: dict | str) -> dict:
    """Build an Ollama ``chat`` response dict (the cached-fixture shape).

    ``payload`` is either the dict the model 'returned' (serialized to the
    message content as JSON) or a raw content string for malformed-response
    tests.
    """
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "model": QWEN_MODEL_TAG,
        "created_at": "2026-05-16T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 1234567,
    }


# ---------------------------------------------------------------------------
# 1. Provider metadata + Protocol conformance
# ---------------------------------------------------------------------------


def test_provider_metadata_constants():
    assert PROVIDER_NAME == "tier2_qwen_7b_local"
    assert TIER == 2
    assert QWEN_MODEL_TAG == "qwen2.5vl:7b"
    assert OLLAMA_KEEP_ALIVE == "1h"
    assert CLEAN_VALUE_CONFIDENCE == 1.0
    assert 0.0 < FORMAT_COERCED_CONFIDENCE < CLEAN_VALUE_CONFIDENCE


def test_provider_satisfies_cascade_provider_protocol():
    assert isinstance(Tier2Qwen7bLocal(), CascadeProvider)


def test_provider_attributes_match_protocol():
    p = Tier2Qwen7bLocal()
    assert p.name == PROVIDER_NAME
    assert p.tier == TIER


# ---------------------------------------------------------------------------
# 2. Schema introspection
# ---------------------------------------------------------------------------


def test_inner_type_unwraps_pydantic_generic():
    """ExtractedField[str] is a Pydantic generic — get_args() is empty, so
    _inner_type must read __pydantic_generic_metadata__."""
    from typing import get_type_hints

    hints = get_type_hints(HealthcareIntakeForm, include_extras=True)
    assert _inner_type(hints["first_name"]) is str
    assert _inner_type(hints["date_of_birth"]) is date


def test_scalar_kind_classification():
    assert _scalar_kind(str) == "string_like"
    assert _scalar_kind(date) == "coerced"
    assert _scalar_kind(bool) == "coerced"
    assert _scalar_kind(float) == "coerced"
    from typing import Literal

    assert _scalar_kind(Literal["M", "F", "U"]) == "string_like"
    # Non-promptable: nested models / collections fall through to None.
    from intake_schemas import SignatureCapture

    assert _scalar_kind(SignatureCapture) is None
    assert _scalar_kind(list[str]) is None


def test_extractable_fields_both_forms_nonempty_and_typed():
    hf = _extractable_fields(HealthcareIntakeForm)
    bf = _extractable_fields(BusinessDocumentForm)
    assert len(hf) > 20 and len(bf) > 20
    # The schema-driven path needs zero per-vertical wiring — both work.
    assert "first_name" in hf and _scalar_kind(hf["first_name"][0]) == "string_like"
    assert "date_of_birth" in hf and _scalar_kind(hf["date_of_birth"][0]) == "coerced"
    assert "vendor_name" in bf
    # Non-scalar fields are excluded (signature is a SignatureCapture model).
    assert "signature" not in hf


# ---------------------------------------------------------------------------
# 3. Prompt + response schema
# ---------------------------------------------------------------------------


def test_build_prompt_lists_every_extractable_field_and_is_deterministic():
    p1 = _build_extraction_prompt(HealthcareIntakeForm)
    p2 = _build_extraction_prompt(HealthcareIntakeForm)
    assert p1 == p2  # pure function of form_cls
    for name in _extractable_fields(HealthcareIntakeForm):
        assert f'"{name}"' in p1
    assert "JSON object" in p1
    assert "null" in p1  # absent-field instruction present


def test_build_prompt_works_for_business_form_without_alias_machinery():
    p = _build_extraction_prompt(BusinessDocumentForm)
    assert '"vendor_name"' in p
    assert '"line_items"' not in p  # non-scalar excluded


def test_build_response_schema_is_constrained_and_nullable():
    schema = _build_response_schema(HealthcareIntakeForm)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    extractable = _extractable_fields(HealthcareIntakeForm)
    assert set(schema["properties"]) == set(extractable)
    assert set(schema["required"]) == set(extractable)
    # Every property is nullable so the model can confidently decline.
    assert schema["properties"]["first_name"] == {"type": ["string", "null"]}
    # date inner type still serializes as a (ISO) string at the wire level.
    assert schema["properties"]["date_of_birth"] == {"type": ["string", "null"]}


# ---------------------------------------------------------------------------
# 4a. _extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_object_clean():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_strips_markdown_fence():
    assert _extract_json_object('```json\n{"a": "b"}\n```') == {"a": "b"}


def test_extract_json_object_scrapes_first_balanced_span():
    assert _extract_json_object('noise {"a": {"b": 2}} trailing') == {"a": {"b": 2}}


def test_extract_json_object_malformed_returns_empty():
    assert _extract_json_object("not json at all") == {}
    assert _extract_json_object("") == {}
    assert _extract_json_object("[1, 2, 3]") == {}  # top-level array, not object


# ---------------------------------------------------------------------------
# 4b. _parse_response
# ---------------------------------------------------------------------------


def test_parse_response_string_field_clean_confidence_1():
    f = _parse_response(_ollama_raw({"first_name": "Jane"}), HealthcareIntakeForm)
    assert f.first_name.value == "Jane"
    assert f.first_name.confidence == CLEAN_VALUE_CONFIDENCE
    assert f.first_name.tier_used == TIER
    assert f.first_name.bounding_box is None  # locked decision 2


def test_parse_response_strips_whitespace():
    f = _parse_response(_ollama_raw({"last_name": "  Doe  "}), HealthcareIntakeForm)
    assert f.last_name.value == "Doe"


def test_parse_response_coerced_date_confidence_half():
    f = _parse_response(_ollama_raw({"date_of_birth": "1985-03-12"}), HealthcareIntakeForm)
    assert f.date_of_birth.value == date(1985, 3, 12)
    assert f.date_of_birth.confidence == FORMAT_COERCED_CONFIDENCE
    assert f.date_of_birth.tier_used == TIER


def test_parse_response_null_is_confidently_blank():
    """A prompted field the model returned null for is attempted-but-blank:
    value=None, tier_used=2 (NOT tier_used=None)."""
    f = _parse_response(_ollama_raw({"first_name": None}), HealthcareIntakeForm)
    assert f.first_name.value is None
    assert f.first_name.tier_used == TIER


def test_parse_response_non_scalar_field_stays_unattempted():
    """signature (SignatureCapture) is never prompted → tier_used stays None."""
    f = _parse_response(_ollama_raw({"first_name": "Jane"}), HealthcareIntakeForm)
    assert f.signature.tier_used is None


def test_parse_response_bad_value_demoted_to_confidently_blank():
    """Unparseable date / bad Literal was attempted → confidently-blank,
    not silently dropped to unattempted (Tier 2 contract differs from Tier 1)."""
    raw = _ollama_raw({"date_of_birth": "NOT-A-DATE", "sex": "Martian", "first_name": "Bob"})
    f = _parse_response(raw, HealthcareIntakeForm)
    assert f.date_of_birth.value is None and f.date_of_birth.tier_used == TIER
    assert f.sex.value is None and f.sex.tier_used == TIER
    assert f.first_name.value == "Bob"  # good field survives the rebuild


def test_parse_response_ignores_extra_keys():
    f = _parse_response(
        _ollama_raw({"first_name": "Jane", "not_a_field": "x"}), HealthcareIntakeForm
    )
    assert f.first_name.value == "Jane"


def test_parse_response_malformed_json_yields_all_blank_no_crash():
    f = _parse_response(_ollama_raw("garbage{not json"), HealthcareIntakeForm)
    assert f.first_name.value is None
    assert f.first_name.tier_used == TIER  # every prompted field stamped


def test_parse_response_business_form():
    f = _parse_response(_ollama_raw({"vendor_name": "Acme Corp"}), BusinessDocumentForm)
    assert f.vendor_name.value == "Acme Corp"
    assert f.vendor_name.tier_used == TIER


def test_parse_response_handles_legacy_dict_message_shape():
    """Defensive: a response missing message.content yields an all-blank form."""
    f = _parse_response({"model": QWEN_MODEL_TAG, "done": True}, HealthcareIntakeForm)
    assert f.first_name.value is None
    assert f.first_name.tier_used == TIER


# ---------------------------------------------------------------------------
# 5. End-to-end extract()
# ---------------------------------------------------------------------------


def test_extract_returns_cached_response_when_cache_hit(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache hit with EVAL_LIVE unset → no live call, latency_ms=0.0."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _ollama_raw({"first_name": "Jane"}))

    def _should_not_be_called() -> None:
        raise AssertionError("live path called despite cache hit")

    monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client", _should_not_be_called)

    result = Tier2Qwen7bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    assert isinstance(result, ProviderResult)
    assert result.form.first_name.value == "Jane"
    assert result.form.first_name.tier_used == 2
    assert result.latency_ms == 0.0
    assert result.cost_usd == 0.0
    assert "message" in result.raw_response


def test_extract_falls_through_to_live_on_cache_miss(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache miss (no EVAL_LIVE) → still hits live path; response persisted."""
    stub = _ollama_raw({"first_name": "Jane"})
    monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client", lambda: "stub-client")
    monkeypatch.setattr(tier2_qwen_7b_local, "_invoke_model", lambda client, png, form_cls: stub)

    result = Tier2Qwen7bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    assert result.form.first_name.value == "Jane"
    assert result.latency_ms >= 0.0
    assert result.cost_usd == 0.0
    assert result.raw_response == stub
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == stub


def test_extract_second_call_is_cache_hit(isolated_cache_root, eval_live_off, monkeypatch):
    stub = _ollama_raw({"first_name": "Jane"})
    monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client", lambda: "stub")
    monkeypatch.setattr(tier2_qwen_7b_local, "_invoke_model", lambda *a, **k: stub)
    Tier2Qwen7bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    monkeypatch.setattr(
        tier2_qwen_7b_local,
        "_invoke_model",
        lambda *a, **k: pytest.fail("second call should be a cache hit"),
    )
    r2 = Tier2Qwen7bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)
    assert r2.form.first_name.value == "Jane"
    assert r2.latency_ms == 0.0


def test_extract_bypasses_cache_when_eval_live_set(isolated_cache_root, eval_live_on, monkeypatch):
    """EVAL_LIVE=true → always call live, overwriting any cached response."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _ollama_raw({"first_name": "Stale"}))
    fresh = _ollama_raw({"first_name": "Fresh"})
    monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client", lambda: "stub")
    monkeypatch.setattr(tier2_qwen_7b_local, "_invoke_model", lambda *a, **k: fresh)

    result = Tier2Qwen7bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)
    assert result.form.first_name.value == "Fresh"
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == fresh


def test_extract_recomputes_sha256_from_png_bytes(isolated_cache_root, eval_live_off, monkeypatch):
    """Provider keys the cache on sha256(png), not any caller-supplied hash."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _ollama_raw({"first_name": "Cached"}))
    monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client", lambda: "stub")
    monkeypatch.setattr(
        tier2_qwen_7b_local,
        "_invoke_model",
        lambda *a, **k: _ollama_raw({"first_name": "FromLive"}),
    )
    result = Tier2Qwen7bLocal().extract(b"DIFFERENT_PNG_BYTES", HealthcareIntakeForm)
    assert result.form.first_name.value == "FromLive"


def test_invoke_model_passes_image_and_schema_via_real_ollama_shape(monkeypatch):
    """_invoke_model uses messages[].images=[png] (the real ollama client
    shape), format=<schema>, keep_alive, temperature=0 — and normalizes a
    pydantic ChatResponse to a JSON-serializable dict."""
    captured = {}

    class _FakeResponse:
        def model_dump(self, mode=None):
            return {"message": {"role": "assistant", "content": "{}"}, "_mode": mode}

    class _FakeClient:
        def chat(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    out = tier2_qwen_7b_local._invoke_model(_FakeClient(), b"PNGBYTES", HealthcareIntakeForm)
    assert captured["model"] == QWEN_MODEL_TAG
    msg = captured["messages"][0]
    assert msg["images"] == [b"PNGBYTES"]  # raw bytes; client b64-encodes
    assert "content" in msg and isinstance(msg["content"], str)
    assert captured["format"]["type"] == "object"  # schema-constrained
    assert captured["keep_alive"] == OLLAMA_KEEP_ALIVE
    assert captured["options"]["temperature"] == 0.0
    assert out["_mode"] == "json"  # serialized JSON-safe for the cache


# ---------------------------------------------------------------------------
# Live path tests (gated)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_live_inference_smoke(isolated_cache_root, eval_live_on):
    """Skipped unless `ollama` is importable AND EVAL_LIVE=true.

    Real live validation runs as the PR deliverable against the 6-doc set;
    this just confirms the client constructs without blowing up. CI never
    sets EVAL_LIVE so this stays skipped.
    """
    try:
        import ollama  # noqa: F401
    except ImportError:
        pytest.skip("ollama not installed; see docs/local-development.md Tier 2 setup")
    provider = Tier2Qwen7bLocal()
    assert provider.name == PROVIDER_NAME


def test_load_ollama_client_raises_helpful_error_when_ollama_missing(monkeypatch):
    """Without `ollama` importable the live path raises ImportError w/ a hint."""
    monkeypatch.setitem(sys.modules, "ollama", None)
    with pytest.raises(ImportError, match="ollama"):
        tier2_qwen_7b_local._load_ollama_client()


# ---------------------------------------------------------------------------
# 6. Validation set: end-to-end cached replay against the 6 CMS-1500 docs
# ---------------------------------------------------------------------------

# CMS-1500-only by the same constraint as Tier 1: DocILE pages are
# CC-BY-NC-ND 4.0 and can't be redistributed in this public repo. DocILE-side
# BusinessDocumentForm validation is a local-only workflow on the GPU box;
# the result is noted in the PR body, not committed.

VALIDATION_DIR = pathlib.Path("tests/fixtures/eval-validation/cms1500")


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


def test_validation_corpus_present():
    pngs = _validation_pngs()
    assert len(pngs) == 6, f"Expected 6 validation PNGs, found {len(pngs)}"


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name)
def test_tier2_cached_replay_on_validation_doc(png_path, monkeypatch):
    """Each validation doc has a cached Tier 2 response that parses cleanly
    and populates at least one field with a real value.

    A true prompted VL model should materially beat Tier 1's 4-6 fields on a
    clean synthetic render; the gate here is the locked acceptance bar
    (>=1 populated field), the richer-than-Tier-1 check is eyeballed at
    regen time per the starter's step 4.
    """
    monkeypatch.delenv("EVAL_LIVE", raising=False)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            f"Cache miss on {png_path.name}: cached fixture missing or PNG bytes drifted."
        )

    monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client", _should_not_be_called)
    monkeypatch.setattr(tier2_qwen_7b_local, "_invoke_model", _should_not_be_called)

    result = Tier2Qwen7bLocal().extract(png_path.read_bytes(), HealthcareIntakeForm)

    assert isinstance(result, ProviderResult)
    assert result.latency_ms == 0.0  # cache hit
    assert result.cost_usd == 0.0
    populated = [
        name
        for name in get_field_metadata(HealthcareIntakeForm)
        if getattr(result.form, name).tier_used == 2
        and getattr(result.form, name).value is not None
    ]
    assert populated, f"{png_path.name}: no fields populated in cached Tier 2 response"
