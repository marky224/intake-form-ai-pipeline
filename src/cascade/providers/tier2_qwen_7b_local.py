"""Tier 2 Qwen 2.5 VL 7B local provider.

Pinned model: **``qwen2.5vl:7b``** pulled from the Ollama registry. Tier 3
likewise uses a registry pull (``qwen2.5vl:32b``, Q4_K_M) — the originally
planned Mungert custom-Modelfile import proved infeasible on the build box
(see ``tier3_qwen_32b_local`` docstring). Served by a local ``ollama serve``
on ``127.0.0.1:11434``.

**Device pin:** RTX 4080 (GPU 0). GPU 1 (RTX 4060 Ti) runs Tier 1
PaddleOCR-VL. The tier-batched eval pattern means Tier 2 and Tier 3 don't
actually need to be co-resident for this PR; the provider just asks Ollama to
``keep_alive`` the model for an hour so it doesn't unload between consecutive
documents in an eval batch.

**Role in the cascade:** unlike Tier 1 (a layout parser — see
``tier1_paddleocr_local``), Tier 2 is a **true prompted VL model**. The
provider builds an extraction prompt from ``form_cls``'s canonical field
names + descriptions, asks Qwen for a JSON object, and parses it. There is no
alias-table post-processor here — extraction is schema-driven, so the same
provider works for both ``HealthcareIntakeForm`` (CMS-1500) and
``BusinessDocumentForm`` (DocILE) with no per-vertical wiring.

**Shared core:** Tier 2 and Tier 3 (``tier3_qwen_32b_local``) are the same
model family (Qwen 2.5 VL, 7B → 32B). All prompt-building, response-schema
construction, JSON parsing, and the confidence heuristic live in
``cascade.providers._qwen_vl`` and are shared verbatim — this module is just
the 7B constants + a thin Protocol-conforming class + the ``_invoke_model`` /
``_load_ollama_client`` test seams. Escalation 7B → 32B is "more
parameters," not "different model."

**Cost:** $0.00/call (local inference).

Four design decisions, locked for V1 (Phase 4 PR (c-V1), 2026-05-16;
implemented in ``_qwen_vl`` from PR (d-V1), 2026-05-16):

1. **Per-field confidence is a deterministic heuristic, not model self-report.**
   The inner type of ``ExtractedField[T]`` decides it statically: a
   string-like field (``str`` / ``Literal[...]``) whose value survives
   Pydantic validation is used verbatim → ``confidence=1.0``. A non-string
   scalar (``date`` / ``int`` / ``float`` / ``bool``) is format-coerced from
   the model's string → ``confidence=0.5``. A field the model returned null
   for, or whose value failed Pydantic, is stamped confidently-blank. This
   keeps the cached-replay path bit-identical to the live path and gives
   Phase 5's 0.85/0.80 escalation thresholds a signal that doesn't depend on
   a 7B model grading its own homework. (Mirrors Tier 1's "confidence is the
   match score, not a model-reported number" posture.)

2. **``bounding_box=None`` for every Tier 2 field.** 7B bbox grounding is
   unreliable; Tier 1 already attaches real layout-parser bboxes to the
   fields it populates, and in the real cascade Tier 2 only fires on the
   fields Tier 1 *couldn't* get. Honest absence beats confidently-wrong
   boxes polluting the reviewer overlay / eval IoU.

3. **Schema-constrained decoding.** The Ollama ``chat`` ``format=`` parameter
   is handed a JSON schema derived from ``form_cls`` (``format`` accepts a
   plain schema dict on the installed client). This nearly eliminates the
   malformed-JSON failure mode — the single biggest robustness lever for a
   7B model. ``parse_response`` still tolerates extra/missing/null keys so
   a non-conforming response degrades to confidently-blank rather than
   crashing the form (same drop-bad-fields discipline as Tier 1).

4. **Full ``form_cls`` extraction.** Phase 4 providers are standalone
   single-shot callables; the prompt covers the whole form. Phase 5's
   orchestrator narrows the prompt to the Tier-1 gap set later — the frozen
   ``extract(png, form_cls)`` Protocol signature does not change here.

**Confidently-blank contract:** every *prompted* (scalar) field is stamped
``tier_used=2`` even when the model returns null or an unparseable value
(``value=None``, ``tier_used=2``) — ``compute_form_confidence`` distinguishes
"never attempted" from "attempted, blank" by exactly that signal, same
contract Tier 1 follows for the fields it touches. Non-scalar fields
(``SignatureCapture``, ``list[...]``) are *not* prompted — a 7B text
extractor can't reliably emit them — so they stay unattempted
(``tier_used=None``); the signature pipeline / Phase 5 owns those.

**No retry / no escalation here.** Phase 4 ships providers as standalone
single-shot callables. Per-tier retry-then-escalate, confidence-threshold
escalation, and the schema-fail-retry-stricter loop are Phase 5 orchestrator
work. On any parse failure this provider returns a form with the prompted
fields confidently-blank — it does not loop.

**Lazy import:** the ``ollama`` client is imported inside
``_load_ollama_client`` (not at module top) so the ``cascade`` package
imports cleanly even if the dep is stripped, mirroring Tier 1's structure.
Cached-replay tests never touch the live path; only the ``EVAL_LIVE=true``
workflow on the build machine reaches Ollama.
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
    "Tier2Qwen7bLocal",
]

#: Stable provider identifier. Used as the eval-cache subdirectory name
#: (``tests/fixtures/eval-cache/tier2_qwen_7b_local/<sha>.json``).
PROVIDER_NAME = "tier2_qwen_7b_local"

#: Cascade tier per ``intake_schemas.TierId``.
TIER: TierId = 2

#: Ollama model tag. ``ollama pull qwen2.5vl:7b`` (registry Q8_0 build).
#: Bumping this string is what re-pins the checkpoint.
QWEN_MODEL_TAG = "qwen2.5vl:7b"

#: Pipeline version stamped onto stub ``FormMetadata``. Phase 5's orchestrator
#: overrides this with the live pipeline version; the stub keeps the form
#: instantiable since FormMetadata.pipeline_version is required.
PIPELINE_VERSION = f"tier2-qwen2.5-vl-7b@{TIER}"


def _load_ollama_client() -> Any:
    """Lazy ``ollama`` import pinned to the local host. Memoized by the class.

    Thin wrapper over ``_qwen_vl.load_ollama_client`` so the tier's test file
    keeps its ``monkeypatch.setattr(tier2_qwen_7b_local, "_load_ollama_client")``
    seam and the ImportError can point at the Tier 2 setup docs. The
    cached-replay path never reaches this code, so CI doesn't need a running
    Ollama server.
    """
    try:
        return _qwen_vl.load_ollama_client(OLLAMA_HOST)
    except ImportError as e:
        raise ImportError(
            "Tier 2 live inference requires the `ollama` client and a running "
            "`ollama serve`. See docs/local-development.md `Local Tier 2 model "
            "setup (Qwen 2.5 VL 7B)`. Cached-replay tests do not require this."
        ) from e


def _invoke_model(client: Any, png: bytes, form_cls: type[T]) -> dict[str, Any]:
    """Run one Qwen 2.5 VL 7B chat completion. Tests stub via ``monkeypatch``.

    Thin wrapper over ``_qwen_vl.invoke_model`` pinning the 7B tag; see that
    function for the real Ollama API shape (``messages[].images=[png]``,
    ``format=<schema>``, ``keep_alive``).
    """
    return _qwen_vl.invoke_model(client, png, form_cls, model_tag=QWEN_MODEL_TAG)


class Tier2Qwen7bLocal:
    """Conforms to ``cascade.providers._base.CascadeProvider``.

    Thin shell: state is just the lazily-constructed Ollama client. All
    prompt-building and parsing lives in ``cascade.providers._qwen_vl`` so
    the cached-replay path and the live path share one parser with Tier 3.
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
        ``Tier1PaddleOcrLocal.extract`` and ``Tier3Qwen32bLocal.extract``.

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
