"""ColQwen 2.5 embedding pipeline — FP16, EVAL_LIVE-gated, ``.npy`` cached.

ColQwen 2.5 (``vidore/colqwen2.5-v0.2``, a Qwen2.5-VL-3B late-interaction
retriever) produces a multivector matrix per page image. It runs as a
**separate batch pass with the cascade idle**, so its ~6 GB FP16/bf16
footprint never co-resides with Tier 2/3 — the 32 GB combined-VRAM budget
is not a constraint here (the build decision, surfaced to and confirmed by
Mark at Phase 8 entry; quantizing a 3B retriever would only trade retrieval
quality for VRAM we never contend for).

Replay contract — identical in spirit to the cascade providers'
``eval_cache``, but the payload is a float32 matrix so it is cached as
``.npy`` rather than JSON::

    tests/fixtures/eval-cache/colqwen2.5/<image_sha256>.npy

Default (``EVAL_LIVE`` unset): load the committed ``.npy`` fixture — no
GPU, no ``colpali-engine`` import, $0, deterministic in CI. ``EVAL_LIVE=true``:
load the model, embed for real, write the fixture back. A cache miss
without ``EVAL_LIVE`` raises :class:`EmbeddingUnavailable` so callers (the
demo / retrieval surface) degrade cleanly instead of importing torch.

``colpali-engine`` is **not** a declared dependency (same rationale as
``paddlepaddle-gpu``: it drags the full torch stack and is only ever
imported on the GPU box under ``EVAL_LIVE``). It is lazy-imported inside
:func:`_load_model`; the module stays importable — and CI-testable via the
cached path + synthetic matrices — without it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cascade.eval_cache import CACHE_ROOT, is_live_mode

if TYPE_CHECKING:
    import numpy as np

#: Cache slug (parallels a provider name). ``.npy`` files live here.
EMBED_PROVIDER_NAME = "colqwen2.5"

#: The locked V1 retrieval model. ``vidore/colqwen2.5-v0.2`` is the current
#: stable ColQwen 2.5 checkpoint on the HF hub.
COLQWEN_MODEL = "vidore/colqwen2.5-v0.2"

#: Process-wide model handle, lazily built once under EVAL_LIVE.
_MODEL: Any = None
_PROCESSOR: Any = None


class EmbeddingUnavailable(RuntimeError):
    """No cached embedding for this image and ``EVAL_LIVE`` is not set.

    Callers that have a non-GPU fallback (the demo's retrieval panel)
    catch this and degrade; callers that require an embedding let it
    propagate (a misconfigured ``EVAL_LIVE`` run should fail loudly).
    """


def embed_cache_path(image_sha256: str) -> Path:
    """``.npy`` fixture path for an image hash (mirrors ``eval_cache``)."""
    if len(image_sha256) != 64 or not all(c in "0123456789abcdef" for c in image_sha256):
        raise ValueError(f"image_sha256 must be 64 lowercase hex chars, got {image_sha256!r}")
    return CACHE_ROOT / EMBED_PROVIDER_NAME / f"{image_sha256}.npy"


def _load_model() -> tuple[Any, Any]:
    """Lazily construct the ColQwen 2.5 model + processor (GPU, EVAL_LIVE).

    Imported here so the module is importable without ``colpali-engine`` /
    ``torch``. bf16 on a single CUDA device — the cascade is idle during the
    embedding pass so the whole GPU is free.
    """
    global _MODEL, _PROCESSOR
    if _MODEL is not None:
        return _MODEL, _PROCESSOR
    import torch
    from colpali_engine.models import ColQwen2_5, ColQwen2_5_Processor

    _MODEL = ColQwen2_5.from_pretrained(
        COLQWEN_MODEL,
        torch_dtype=torch.bfloat16,
        device_map="cuda:0",
    ).eval()
    _PROCESSOR = ColQwen2_5_Processor.from_pretrained(COLQWEN_MODEL)
    return _MODEL, _PROCESSOR


def _embed_live(png: bytes) -> np.ndarray:
    """Run ColQwen 2.5 on one page image → ``(tokens, dim)`` float32.

    Only ever reached under ``EVAL_LIVE`` on the GPU box. Torch / PIL are
    imported lazily alongside the model.
    """
    import io

    import numpy as np
    import torch
    from PIL import Image

    model, processor = _load_model()
    image = Image.open(io.BytesIO(png)).convert("RGB")
    batch = processor.process_images([image]).to(model.device)
    with torch.no_grad():
        out = model(**batch)
    # ColQwen returns (batch, tokens, dim); take the single image, drop to
    # float32 numpy on the host for storage + NumPy MaxSim.
    return out[0].to(torch.float32).cpu().numpy().astype(np.float32)


def embed_image(png: bytes) -> np.ndarray:
    """Multivector embedding for a page image, cached/$0 by default.

    ``EVAL_LIVE`` unset → return the committed ``.npy`` fixture; raise
    :class:`EmbeddingUnavailable` on a miss (no torch import). ``EVAL_LIVE``
    set → embed live and persist the fixture.
    """
    import numpy as np

    sha = hashlib.sha256(png).hexdigest()
    path = embed_cache_path(sha)

    if not is_live_mode():
        if not path.is_file():
            raise EmbeddingUnavailable(
                f"no cached ColQwen embedding for {sha[:12]}… and EVAL_LIVE is unset. "
                f"Run `EVAL_LIVE=true just embed` on the GPU box to generate it."
            )
        return np.load(path).astype(np.float32)

    mat = _embed_live(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, mat)
    return mat
