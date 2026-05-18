"""One-time live regen of the committed eval-cache fixtures.

Generates, for every ``test``-split doc in ``evals/manifest.json``, the
cached raw responses for all four replay namespaces:

  tier1_paddleocr_local · tier2_qwen_7b_local · router_stage2_qwen_7b ·
  tier3_qwen_32b_local

Each component's live path auto-writes its fixture under ``EVAL_LIVE=true``
(``cascade.eval_cache.save_cached``). We invoke every component
*unconditionally* per doc (not via the orchestrator's routing/escalation)
so that the cached 9-batch sweep never misses a fixture regardless of which
alias batch escalates which doc.

Ordered by model to avoid VRAM thrash: Tier 1 (PaddleOCR-VL, GPU 1) →
Tier 2 + router Stage 2 (both Qwen 7B, Ollama) → Tier 3 (Qwen 32B, Ollama).

Resumable: a (component, doc) whose fixture already exists is skipped, so a
re-run continues a partial Tier-3 pass. Per-doc failures are logged and
counted; the script exits non-zero if any occurred (re-run to fill gaps).

    EVAL_LIVE=true uv run python -m scripts.regen_eval_fixtures
"""

from __future__ import annotations

import logging
import sys

from cascade import eval_cache, router
from cascade.providers.tier1_paddleocr_local import PROVIDER_NAME as T1
from cascade.providers.tier1_paddleocr_local import Tier1PaddleOcrLocal
from cascade.providers.tier2_qwen_7b_local import PROVIDER_NAME as T2
from cascade.providers.tier2_qwen_7b_local import Tier2Qwen7bLocal
from cascade.providers.tier3_qwen_32b_local import PROVIDER_NAME as T3
from cascade.providers.tier3_qwen_32b_local import Tier3Qwen32bLocal
from cascade.router import STAGE2_PROVIDER_NAME as RS2
from evals.manifest import CMS1500_VALIDATION_DIR, load_manifest
from intake_schemas import HealthcareIntakeForm

logger = logging.getLogger("regen")


def _test_pngs() -> list[tuple[str, bytes, str]]:
    """``(doc_id, png_bytes, image_sha256)`` for every test-split doc."""
    _, entries = load_manifest()
    out = []
    for e in sorted((e for e in entries if e.split == "test"), key=lambda e: e.doc_id):
        png = (CMS1500_VALIDATION_DIR / f"{e.doc_id}.png").read_bytes()
        out.append((e.doc_id, png, e.image_sha256))
    return out


def _pass(label: str, slug: str, docs, fn) -> int:
    """Run one component over all docs; skip cached, log+count failures."""
    failures = 0
    total = len(docs)
    for i, (doc_id, png, sha) in enumerate(docs, 1):
        if eval_cache.cache_path(slug, sha).is_file():
            logger.info("%s %d/%d %s — skip (cached)", label, i, total, doc_id)
            continue
        try:
            info = fn(png)
            logger.info("%s %d/%d %s — %s", label, i, total, doc_id, info)
        except Exception as exc:  # batch tool: log, continue, exit≠0
            failures += 1
            logger.error("%s %d/%d %s — FIXERR %r", label, i, total, doc_id, exc)
    return failures


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not eval_cache.is_live_mode():
        logger.error("EVAL_LIVE is not set — refusing to run (this is the live regen).")
        return 2

    docs = _test_pngs()
    logger.info("regen over %d test-split docs x 4 namespaces", len(docs))

    t1, t2, t3 = Tier1PaddleOcrLocal(), Tier2Qwen7bLocal(), Tier3Qwen32bLocal()

    def t1_fn(png: bytes) -> str:
        r = t1.extract(png, HealthcareIntakeForm)
        return f"blocks={len(r.raw_response.get('parsing_res_list', []))}"

    def t2_fn(png: bytes) -> str:
        r = t2.extract(png, HealthcareIntakeForm)
        return f"latency={r.latency_ms:.0f}ms"

    def rs2_fn(png: bytes) -> str:
        return f"vertical={router._stage2_classify(png)}"

    def t3_fn(png: bytes) -> str:
        r = t3.extract(png, HealthcareIntakeForm)
        return f"latency={r.latency_ms:.0f}ms"

    failures = 0
    failures += _pass("TIER1", T1, docs, t1_fn)
    failures += _pass("TIER2", T2, docs, t2_fn)
    failures += _pass("RSTG2", RS2, docs, rs2_fn)
    failures += _pass("TIER3", T3, docs, t3_fn)

    if failures:
        logger.error("DONE with %d failure(s) — re-run to fill gaps", failures)
        return 1
    logger.info("DONE — all 4 namespaces regenerated for %d docs", len(docs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
