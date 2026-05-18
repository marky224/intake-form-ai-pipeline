"""Tests for ``cascade.providers.tier3_qwen_32b_local``.

The shared Qwen-VL core (prompt / response-schema / JSON parse / confidence
heuristic) is tested in ``test__qwen_vl`` against both the 7B and 32B tiers
(including the lettered ``tier="3a"`` flow). This file covers only the 32B
tier's own surface, mirroring ``test_tier2_qwen_7b_local``:

1. **Provider shape** — Protocol conformance + metadata constants
   (lettered ``tier="3a"``, the registry ``qwen2.5vl:32b`` model tag).
2. **End-to-end ``extract()``** — cached-replay + live-path stub +
   ``EVAL_LIVE`` bypass + sha256 cache-key correctness, with the ``"3a"``
   tier stamped end-to-end.
3. **``_invoke_model`` seam** — real ollama client shape, 32B tag.
4. **``_load_ollama_client`` seam** — helpful ImportError when ollama missing.
5. **Validation set** — the 92-doc CMS-1500 `test`-split cached fixtures
   (generated against the locked registry ``qwen2.5vl:32b`` Q4_K_M build) must parse
   cleanly with at least one *populated* field each.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

import pytest

from cascade import eval_cache
from cascade.providers import tier3_qwen_32b_local
from cascade.providers._base import CascadeProvider, ProviderResult
from cascade.providers.tier3_qwen_32b_local import (
    CLEAN_VALUE_CONFIDENCE,
    FORMAT_COERCED_CONFIDENCE,
    OLLAMA_KEEP_ALIVE,
    PROVIDER_NAME,
    QWEN_MODEL_TAG,
    TIER,
    Tier3Qwen32bLocal,
)
from intake_schemas import HealthcareIntakeForm, get_field_metadata

_FAKE_PNG = b"PNG_PAYLOAD_for_tier3_tests"
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
    assert PROVIDER_NAME == "tier3_qwen_32b_local"
    assert TIER == "3a"  # lettered str member, not an int like Tier 1/2
    assert QWEN_MODEL_TAG == "qwen2.5vl:32b"  # registry Q4_K_M (locked 2026-05-17)
    assert OLLAMA_KEEP_ALIVE == "1h"
    assert CLEAN_VALUE_CONFIDENCE == 1.0
    assert 0.0 < FORMAT_COERCED_CONFIDENCE < CLEAN_VALUE_CONFIDENCE


def test_provider_satisfies_cascade_provider_protocol():
    assert isinstance(Tier3Qwen32bLocal(), CascadeProvider)


def test_provider_attributes_match_protocol():
    p = Tier3Qwen32bLocal()
    assert p.name == PROVIDER_NAME
    assert p.tier == TIER
    assert p.tier == "3a"


# ---------------------------------------------------------------------------
# 2. End-to-end extract()
# ---------------------------------------------------------------------------


def test_extract_returns_cached_response_when_cache_hit(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache hit with EVAL_LIVE unset → no live call, latency_ms=0.0, the
    "3a" tier stamped end-to-end."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _ollama_raw({"first_name": "Jane"}))

    def _should_not_be_called() -> None:
        raise AssertionError("live path called despite cache hit")

    monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client", _should_not_be_called)

    result = Tier3Qwen32bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    assert isinstance(result, ProviderResult)
    assert result.form.first_name.value == "Jane"
    assert result.form.first_name.tier_used == "3a"  # lettered tier stamped
    assert result.latency_ms == 0.0
    assert result.cost_usd == 0.0
    assert "message" in result.raw_response


def test_extract_falls_through_to_live_on_cache_miss(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache miss (no EVAL_LIVE) → still hits live path; response persisted."""
    stub = _ollama_raw({"first_name": "Jane"})
    monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client", lambda: "stub-client")
    monkeypatch.setattr(tier3_qwen_32b_local, "_invoke_model", lambda client, png, form_cls: stub)

    result = Tier3Qwen32bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    assert result.form.first_name.value == "Jane"
    assert result.form.first_name.tier_used == "3a"
    assert result.latency_ms >= 0.0
    assert result.cost_usd == 0.0
    assert result.raw_response == stub
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == stub


def test_extract_second_call_is_cache_hit(isolated_cache_root, eval_live_off, monkeypatch):
    stub = _ollama_raw({"first_name": "Jane"})
    monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client", lambda: "stub")
    monkeypatch.setattr(tier3_qwen_32b_local, "_invoke_model", lambda *a, **k: stub)
    Tier3Qwen32bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    monkeypatch.setattr(
        tier3_qwen_32b_local,
        "_invoke_model",
        lambda *a, **k: pytest.fail("second call should be a cache hit"),
    )
    r2 = Tier3Qwen32bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)
    assert r2.form.first_name.value == "Jane"
    assert r2.latency_ms == 0.0


def test_extract_bypasses_cache_when_eval_live_set(isolated_cache_root, eval_live_on, monkeypatch):
    """EVAL_LIVE=true → always call live, overwriting any cached response."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _ollama_raw({"first_name": "Stale"}))
    fresh = _ollama_raw({"first_name": "Fresh"})
    monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client", lambda: "stub")
    monkeypatch.setattr(tier3_qwen_32b_local, "_invoke_model", lambda *a, **k: fresh)

    result = Tier3Qwen32bLocal().extract(_FAKE_PNG, HealthcareIntakeForm)
    assert result.form.first_name.value == "Fresh"
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == fresh


def test_extract_recomputes_sha256_from_png_bytes(isolated_cache_root, eval_live_off, monkeypatch):
    """Provider keys the cache on sha256(png), not any caller-supplied hash."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _ollama_raw({"first_name": "Cached"}))
    monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client", lambda: "stub")
    monkeypatch.setattr(
        tier3_qwen_32b_local,
        "_invoke_model",
        lambda *a, **k: _ollama_raw({"first_name": "FromLive"}),
    )
    result = Tier3Qwen32bLocal().extract(b"DIFFERENT_PNG_BYTES", HealthcareIntakeForm)
    assert result.form.first_name.value == "FromLive"


# ---------------------------------------------------------------------------
# 3. _invoke_model seam
# ---------------------------------------------------------------------------


def test_invoke_model_passes_image_and_schema_via_real_ollama_shape(monkeypatch):
    """_invoke_model uses messages[].images=[png] (the real ollama client
    shape), format=<schema>, keep_alive, temperature=0, the 32B registry
    tag — and normalizes a pydantic ChatResponse to a JSON-serializable dict."""
    captured = {}

    class _FakeResponse:
        def model_dump(self, mode=None):
            return {"message": {"role": "assistant", "content": "{}"}, "_mode": mode}

    class _FakeClient:
        def chat(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

    out = tier3_qwen_32b_local._invoke_model(_FakeClient(), b"PNGBYTES", HealthcareIntakeForm)
    assert captured["model"] == QWEN_MODEL_TAG == "qwen2.5vl:32b"
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
    """Skipped unless `ollama` is importable AND EVAL_LIVE=true. CI never
    sets EVAL_LIVE so this stays skipped."""
    try:
        import ollama  # noqa: F401
    except ImportError:
        pytest.skip("ollama not installed; see docs/local-development.md Tier 3 setup")
    provider = Tier3Qwen32bLocal()
    assert provider.name == PROVIDER_NAME


def test_load_ollama_client_raises_helpful_error_when_ollama_missing(monkeypatch):
    """Without `ollama` importable the live path raises ImportError w/ a hint
    that points at the Tier 3 setup docs."""
    monkeypatch.setitem(sys.modules, "ollama", None)
    with pytest.raises(ImportError, match="Tier 3 live inference"):
        tier3_qwen_32b_local._load_ollama_client()


# ---------------------------------------------------------------------------
# 5. Validation set: end-to-end cached replay against the 92-doc CMS-1500 test split
# ---------------------------------------------------------------------------

# CMS-1500-only by the same constraint as Tier 1/2: DocILE pages are
# CC-BY-NC-ND 4.0 and can't be redistributed in this public repo. DocILE-side
# BusinessDocumentForm validation (part of the 20-doc rescoped quant
# validation) is a local-only workflow on the GPU box; the result is in the
# PR body, not committed.

VALIDATION_DIR = pathlib.Path("tests/fixtures/eval-validation/cms1500")


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


def test_validation_corpus_present():
    """Broad test split: 92 = the deterministic ``test`` partition of the
    locked-seed 584-doc corpus. Canonical invariant in
    ``test_evals_manifest.py::test_validation_dir_is_exactly_the_test_split``."""
    pngs = _validation_pngs()
    assert len(pngs) == 92, f"Expected 92 validation PNGs, found {len(pngs)}"


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name)
def test_tier3_cached_replay_on_validation_doc(png_path, monkeypatch):
    """Each validation doc has a cached Tier 3 response (winning quant) that
    parses cleanly and populates at least one field with a real value.

    A 32B prompted VL model should meet or beat Tier 2's 18-19 fields/doc on
    a clean synthetic render; the gate here is the locked acceptance bar
    (>=1 populated field), the richer-than-Tier-2 check is eyeballed at
    regen time."""
    monkeypatch.delenv("EVAL_LIVE", raising=False)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            f"Cache miss on {png_path.name}: cached fixture missing or PNG bytes drifted."
        )

    monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client", _should_not_be_called)
    monkeypatch.setattr(tier3_qwen_32b_local, "_invoke_model", _should_not_be_called)

    result = Tier3Qwen32bLocal().extract(png_path.read_bytes(), HealthcareIntakeForm)

    assert isinstance(result, ProviderResult)
    assert result.latency_ms == 0.0  # cache hit
    assert result.cost_usd == 0.0
    populated = [
        name
        for name in get_field_metadata(HealthcareIntakeForm)
        if getattr(result.form, name).tier_used == "3a"
        and getattr(result.form, name).value is not None
    ]
    assert populated, f"{png_path.name}: no fields populated in cached Tier 3 response"
