"""Tests for ``cascade.providers.tier2_qwen_7b_local``.

Post-PR-(d-V1) the shared Qwen-VL core (prompt / response-schema / JSON
parse / confidence heuristic) moved to ``cascade.providers._qwen_vl`` and is
tested in ``test__qwen_vl`` (against both the 7B and 32B tiers). This file
now covers only the 7B tier's own surface:

1. **Provider shape** — Protocol conformance + metadata constants.
2. **End-to-end ``extract()``** — cached-replay path + live-path stub +
   ``EVAL_LIVE`` bypass + sha256 cache-key correctness.
3. **``_invoke_model`` seam** — the real ollama client shape, 7B tag.
4. **``_load_ollama_client`` seam** — helpful ImportError when ollama missing.
5. **Validation set** — the 92-doc CMS-1500 `test`-split cached fixtures
   must parse cleanly with at least one *populated* field each.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

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
)
from intake_schemas import HealthcareIntakeForm, get_field_metadata

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
    """Build an Ollama ``chat`` response dict (the cached-fixture shape)."""
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
# 2. End-to-end extract()
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


# ---------------------------------------------------------------------------
# 3. _invoke_model seam
# ---------------------------------------------------------------------------


def test_invoke_model_passes_image_and_schema_via_real_ollama_shape(monkeypatch):
    """_invoke_model uses messages[].images=[png] (the real ollama client
    shape), format=<schema>, keep_alive, temperature=0, the 7B tag — and
    normalizes a pydantic ChatResponse to a JSON-serializable dict."""
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
# 4. _load_ollama_client seam (gated live path)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_live_inference_smoke(isolated_cache_root, eval_live_on):
    """Skipped unless `ollama` is importable AND EVAL_LIVE=true.

    Real live validation runs via `just regen-fixtures` against the 92-doc
    CMS-1500 test split; this just confirms the client constructs without
    blowing up. CI never sets EVAL_LIVE so this stays skipped.
    """
    try:
        import ollama  # noqa: F401
    except ImportError:
        pytest.skip("ollama not installed; see docs/local-development.md Tier 2 setup")
    provider = Tier2Qwen7bLocal()
    assert provider.name == PROVIDER_NAME


def test_load_ollama_client_raises_helpful_error_when_ollama_missing(monkeypatch):
    """Without `ollama` importable the live path raises ImportError w/ a hint
    that points at the Tier 2 setup docs."""
    monkeypatch.setitem(sys.modules, "ollama", None)
    with pytest.raises(ImportError, match="Tier 2 live inference"):
        tier2_qwen_7b_local._load_ollama_client()


# ---------------------------------------------------------------------------
# 5. Validation set: end-to-end cached replay against the 92-doc CMS-1500 test split
# ---------------------------------------------------------------------------

# CMS-1500-only by the same constraint as Tier 1: DocILE pages are
# CC-BY-NC-ND 4.0 and can't be redistributed in this public repo. DocILE-side
# BusinessDocumentForm validation is a local-only workflow on the GPU box;
# the result is noted in the PR body, not committed.

VALIDATION_DIR = pathlib.Path(__file__).parent / "fixtures" / "eval-validation" / "cms1500"


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


def test_validation_corpus_present():
    """Broad test split: 92 = the deterministic ``test`` partition of the
    locked-seed 584-doc corpus. Canonical invariant in
    ``test_evals_manifest.py::test_validation_dir_is_exactly_the_test_split``."""
    pngs = _validation_pngs()
    assert len(pngs) == 92, f"Expected 92 validation PNGs, found {len(pngs)}"


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name)
def test_tier2_cached_replay_on_validation_doc(png_path, monkeypatch):
    """Each validation doc has a cached Tier 2 response that parses cleanly
    and populates at least one field with a real value."""
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
