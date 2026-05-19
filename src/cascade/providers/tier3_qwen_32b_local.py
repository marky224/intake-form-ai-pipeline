"""Tier 3 Qwen 2.5 VL 32B local provider.

V1's terminal cascade tier (was "Tier 3a" in pre-pivot V2 numbering; the
``"3b"`` Bedrock Sonnet frontier-fallback is V2-deferred — V1 has no cloud
tier above this, exhaustion fails to a local review queue).

Pinned model: the **Ollama registry build** ``qwen2.5vl:32b`` (Q4_K_M,
~21 GB). The vision projector stays FP16 in the registry package — only the
LLM weights are quantized, so quantization concerns are about LLM reasoning
over vision features, not feature extraction itself.

**Locked quant: Q4_K_M (registry ``qwen2.5vl:32b``).** The architecture
originally locked a Mungert-GGUF Q8_0 default with a Q8_0-vs-Q6_K dual-quant
sanity test (demote to Q6_K iff F1 gap ≤ ±0.02). That test was **empirically
infeasible on the build box** (`openclaw-pc`, RTX 4080 + RTX 4060 Ti ≈
31.2 GB usable, Ollama 0.20.7) and was rescoped 2026-05-17 (Mark's call):

  - Mungert **Q6_K** (custom dual-``FROM`` Modelfile import, ~29 GB, loads
    100 % GPU at ``num_ctx 8192``/``num_gpu 99`` — fits, no OOM) still fails
    *every* vision inference with the M-RoPE context-shift assert
    ``GGML_ASSERT(n_pos_per_embd()==1 && "seq_add()...")`` (open llama.cpp
    issue #19915). Proven NOT a num_ctx/VRAM problem: only ~2315 ctx tokens
    are needed, 8192 is ample, model is fully GPU-resident — the assert is
    specific to the Mungert GGUF Modelfile-import path on this Ollama; the
    registry build avoids it entirely, independent of ``num_ctx``.
  - Mungert **Q8_0** (~35 GB) exceeds 31.2 GB → heavy CPU spill →
    multi-minute/doc (impractical latency).
  - Registry **Q4_K_M** ``qwen2.5vl:32b`` is the only config that runs
    correctly: ~52 s/doc, clean schema-constrained JSON, ~18 fields/doc on a
    CMS-1500 (verified on `openclaw-pc` 2026-05-17).

The rescoped "dual-quant" deliverable is therefore: document the Mungert
infeasibility + ship Q4_K_M with its measured validation F1, applying the
contingency tree's absolute-F1 branches (>=0.80 ship / 0.65-0.80 document
gap and ship / <0.65 fall back). See ``architecture-locked.md`` "Quantization
choice and contingency tree" for the recorded empirical decision.

**Device:** combined VRAM pool (RTX 4080 + RTX 4060 Ti). Per the project
hardware notes ``can_device_access_peer(0, 1) = False`` on this AM4 board, so
a model-parallel split across the two cards routes through host memory
(~11 GB/s host-staged), not direct PCIe peer DMA. ``keep_alive`` holds the
model resident across an eval batch so the (large) load cost is paid once.

**Role + design:** identical to Tier 2 — a true prompted VL model, schema
driven, no per-vertical wiring, same four locked V1 decisions (deterministic
inner-type confidence heuristic [1.0 string-like / 0.5 coerced], not model
self-report; ``bounding_box=None``; schema-constrained ``format=`` decoding;
full ``form_cls`` extraction) and the same confidently-blank contract
(every prompted scalar stamped ``tier_used="3a"`` even when null/unparseable;
non-scalar fields left unattempted). The full rationale lives on
``tier2_qwen_7b_local``; the shared implementation lives in
``cascade.providers._qwen_vl``. This module is just the 32B constants + a
thin Protocol-conforming class + the ``_invoke_model`` /
``_load_ollama_client`` test seams. Escalation 7B → 32B is "more
parameters," not "different model family."

**Cost:** $0.00/call (local inference).

**No retry / no escalation here.** Phase 4 ships providers as standalone
single-shot callables; per-tier retry-then-escalate and confidence-threshold
escalation are Phase 5 orchestrator work.

**Lazy import:** the ``ollama`` client is imported inside
``_load_ollama_client`` so the ``cascade`` package imports cleanly without
the dep. Cached-replay tests never touch the live path; only the
``EVAL_LIVE=true`` workflow on the build machine reaches Ollama. The
registry 32B honors the same ``format=<json-schema>`` schema-constrained
decoding as the 7B registry build (verified on ``openclaw-pc`` before this
provider was locked — same "check the real upstream API shape" discipline as
the Tier 1 PaddleOCR-VL + Tier 2 lessons; this is also *why* registry was
chosen over the Mungert import, which hits the M-RoPE ``seq_add`` assert).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from cascade import eval_cache
from cascade.providers import _qwen_vl
from cascade.providers._base import ProviderResult, T
from cascade.providers._qwen_vl import (  # re-exported for the tier's test file
    CLEAN_VALUE_CONFIDENCE,
    FORMAT_COERCED_CONFIDENCE,
    OLLAMA_HOST,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_TEMPERATURE,
)
from intake_schemas import TierId

__all__ = [
    "CLEAN_VALUE_CONFIDENCE",
    "FORMAT_COERCED_CONFIDENCE",
    "OLLAMA_HOST",
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_TEMPERATURE",
    "PIPELINE_VERSION",
    "PROVIDER_NAME",
    "QWEN_MODEL_TAG",
    "TIER",
    "Tier3Qwen32bLocal",
]

#: Stable provider identifier. Used as the eval-cache subdirectory name
#: (``tests/fixtures/eval-cache/tier3_qwen_32b_local/<sha>.json``).
PROVIDER_NAME = "tier3_qwen_32b_local"

#: Cascade tier per ``intake_schemas.TierId``. Lettered (a ``str``, not an
#: ``int`` like Tier 1/2) — V1 has only Tier 3a; ``"3b"`` is the V2-deferred
#: Bedrock frontier fallback. ``compute_form_confidence`` and the
#: confidently-blank contract key off ``tier_used is (not) None``, so the
#: ``str`` member flows through identically to the integer tiers.
TIER: TierId = "3a"

#: Ollama model tag — the **registry build** ``qwen2.5vl:32b`` (Q4_K_M), same
#: as Tier 2's ``qwen2.5vl:7b`` pull. Locked 2026-05-17 after the Mungert
#: Q8_0/Q6_K imports proved infeasible on the build box (Q6_K → M-RoPE
#: ``seq_add`` assert regardless of num_ctx/VRAM; Q8_0 → impractical CPU
#: spill). See the module docstring + ``architecture-locked.md``.
QWEN_MODEL_TAG = "qwen2.5vl:32b"

#: Pipeline version stamped onto stub ``FormMetadata``. Phase 5's orchestrator
#: overrides this with the live pipeline version; the stub keeps the form
#: instantiable since FormMetadata.pipeline_version is required.
PIPELINE_VERSION = f"tier3-qwen2.5-vl-32b@{TIER}"


def _load_ollama_client() -> Any:
    """Lazy ``ollama`` import pinned to the local host. Memoized by the class.

    Thin wrapper over ``_qwen_vl.load_ollama_client`` so the tier's test file
    keeps its ``monkeypatch.setattr(tier3_qwen_32b_local, "_load_ollama_client")``
    seam and the ImportError can point at the Tier 3 setup docs. The
    cached-replay path never reaches this code, so CI doesn't need a running
    Ollama server.
    """
    try:
        return _qwen_vl.load_ollama_client(OLLAMA_HOST)
    except ImportError as e:
        raise ImportError(
            "Tier 3 live inference requires the `ollama` client, a running "
            "`ollama serve`, and `ollama pull qwen2.5vl:32b`. See "
            "docs/local-development.md `Local Tier 3 model setup`. "
            "Cached-replay tests do not require this."
        ) from e


def _invoke_model(client: Any, png: bytes, form_cls: type[T]) -> dict[str, Any]:
    """Run one Qwen 2.5 VL 32B chat completion. Tests stub via ``monkeypatch``.

    Thin wrapper over ``_qwen_vl.invoke_model`` pinning the 32B tag; see that
    function for the real Ollama API shape (``messages[].images=[png]``,
    ``format=<schema>``, ``keep_alive``) — identical to Tier 2 (same family).
    """
    return _qwen_vl.invoke_model(client, png, form_cls, model_tag=QWEN_MODEL_TAG)


class Tier3Qwen32bLocal:
    """Conforms to ``cascade.providers._base.CascadeProvider``.

    Thin shell: state is just the lazily-constructed Ollama client. All
    prompt-building and parsing lives in ``cascade.providers._qwen_vl`` so
    the cached-replay path and the live path share one parser with Tier 2.
    """

    name: str = PROVIDER_NAME
    tier: TierId = TIER

    def __init__(self) -> None:
        self._client: Any | None = None

    def _ensure_client_loaded(self) -> Any:
        """Lazy-load the Ollama client. Memoized."""
        if self._client is None:
            self._client = _load_ollama_client()
        return self._client

    def extract(self, png: bytes, form_cls: type[T]) -> ProviderResult[T]:
        """Extract fields from a single-page PNG.

        Cache-first: if ``EVAL_LIVE`` is unset and a cached response exists
        for ``sha256(png)``, parse the cached payload and return with
        ``latency_ms=0.0``. Otherwise call live, persist the response, and
        return real telemetry. Control flow is structurally identical to
        ``Tier1PaddleOcrLocal.extract`` and ``Tier2Qwen7bLocal.extract``.

        ``image_sha256`` is recomputed from ``png`` here (not pulled from a
        sidecar) so a caller bug pairing the wrong PNG with a stale hash
        surfaces as a cache miss rather than serving the wrong cached result.
        """
        image_sha256 = hashlib.sha256(png).hexdigest()

        if not eval_cache.is_live_mode():
            cached = eval_cache.load_cached(PROVIDER_NAME, image_sha256)
            if cached is not None:
                return ProviderResult(
                    form=_qwen_vl.parse_response(
                        cached, form_cls, tier=TIER, pipeline_version=PIPELINE_VERSION
                    ),
                    latency_ms=0.0,
                    cost_usd=0.0,
                    raw_response=cached,
                )

        client = self._ensure_client_loaded()
        t0 = time.perf_counter()
        raw_response = _invoke_model(client, png, form_cls)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        eval_cache.save_cached(PROVIDER_NAME, image_sha256, raw_response)
        return ProviderResult(
            form=_qwen_vl.parse_response(
                raw_response, form_cls, tier=TIER, pipeline_version=PIPELINE_VERSION
            ),
            latency_ms=latency_ms,
            cost_usd=0.0,
            raw_response=raw_response,
        )
