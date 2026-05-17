"""The eval harness: run the cascade over a partition, score, persist.

For each progressive alias batch (1 … ``batch_count``):

1. ``active_alias_batch`` repoints the router vocab + Tier 1 alias map at
   that batch's slice of the seed and clears both caches.
2. Every ``test``-split document is scored at **two stages** (Phase 6
   finding, decided with Mark):

   - ``tier1`` — the pre-escalation form the orchestrator's Tier 1 path
     produces (Tier 1 extract → route → re-parse into the routed class,
     the exact orchestrator lines, reusing its functions). This is the
     layer the alias table governs; its F1 genuinely climbs then asymptotes
     as alias coverage grows. **This is the headline F1-over-time series.**
   - ``cascade`` — the end-to-end ``RunRecord.form`` after escalation.
     Strong Tier 2/3 escalation recovers whatever the alias layer missed,
     so this F1 is ~invariant to alias coverage — persisted as the
     "cascade robustness" stat, not the headline curve.

   Both paths are cached-replay ($0, deterministic in CI);
   ``EVAL_LIVE=true`` drives the live models (handled inside the providers
   — the harness sets no flags).
3. Per-doc counts land in ``eval_results`` (one row per stage); the
   micro-averaged batch lands in ``eval_batches`` (one row per stage).

Healthcare ground truth is the committed CMS-1500 sidecars. DocILE is
local-only: an entry whose PNG/annotation isn't present locally is skipped
with a logged note (CC-BY-NC-ND — never committed, never required in CI).

The OCR layer is replay-cached and form-agnostic, so what actually moves
Tier-1 F1 across batches is the alias-driven Tier 1 parse + router
vocabulary — the genuine self-improvement signal, at $0.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cascade import router
from cascade.orchestrator import build_cascade, process_document
from cascade.providers import tier1_paddleocr_local as tier1_mod
from evals import store as eval_store
from evals.alias_partition import ALIAS_SEED_PATH, active_alias_batch, batch_count, load_seed
from evals.ground_truth import FIELD_KIND, load_cms1500_ground_truth
from evals.manifest import CMS1500_VALIDATION_DIR, ManifestEntry, load_manifest
from evals.metrics import Counts, aggregate, score_form
from intake_schemas import HealthcareIntakeForm

logger = logging.getLogger(__name__)

#: Repo-root-relative thin fixtures manifest (seed + model provenance).
FIXTURES_MANIFEST_PATH = Path(__file__).resolve().parent / "fixtures_manifest.json"

#: Schema fields scored per vertical. Healthcare = the CMS-1500 box-mapped
#: set; business = every BusinessDocumentForm field (DocILE renders the KILE
#: taxonomy verbatim).
_HEALTHCARE_FIELDS = list(FIELD_KIND)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "nogit"


def _business_fields() -> list[str]:
    from intake_schemas import BusinessDocumentForm

    return [n for n in BusinessDocumentForm.model_fields if n != "metadata"]


@dataclass(frozen=True)
class _Scored:
    """One document scored at both stages for one alias batch."""

    vertical: str
    tier1_counts: Counts
    tier1_latency_ms: float
    cascade_counts: Counts
    cascade_latency_ms: float


def _tier1_stage_form(png: bytes, providers: Any) -> tuple[Any, str, float]:
    """The orchestrator's pre-escalation form: Tier 1 → route → re-parse.

    Reuses the exact orchestrator functions (``tier1.extract``,
    ``router.ocr_lines_from_tier1_raw``, ``router.route``,
    ``tier1_mod._parse_response``) so this is genuinely the cascade's Tier-1
    stage under the active alias batch, not a re-implementation. Returns
    ``(form, routed_vertical, tier1_latency_ms)``.
    """
    tier1 = providers[0]
    t1_result = tier1.extract(png, HealthcareIntakeForm)
    ocr_lines = router.ocr_lines_from_tier1_raw(t1_result.raw_response)
    decision = router.route(ocr_lines, png)
    form = tier1_mod._parse_response(t1_result.raw_response, decision.form_cls)
    return form, decision.vertical, t1_result.latency_ms


def _scorable_for(vertical: str) -> list[str]:
    return _HEALTHCARE_FIELDS if vertical == "healthcare" else _business_fields()


def _score_entry(
    entry: ManifestEntry,
    *,
    validation_dir: Path,
    providers: Any,
    db_path: Path | str,
) -> _Scored | None:
    """Run + score one document at both stages. ``None`` if not local."""
    if entry.vertical == "healthcare":
        png_path = validation_dir / f"{entry.doc_id}.png"
        sidecar_path = validation_dir / f"{entry.doc_id}.json"
        if not png_path.is_file() or not sidecar_path.is_file():
            logger.warning("skipping %s — missing CMS-1500 assets", entry.doc_id)
            return None
        truth = load_cms1500_ground_truth(sidecar_path)
    else:
        # DocILE: local-only, CC-BY-NC-ND. Not present in CI → skipped.
        logger.warning("skipping %s — DocILE eval is local-only", entry.doc_id)
        return None

    png = png_path.read_bytes()
    t1_form, t1_vertical, t1_latency = _tier1_stage_form(png, providers)
    tier1_counts = score_form(t1_form, truth, _scorable_for(t1_vertical))

    record = process_document(png, doc_id=entry.doc_id, db_path=db_path, providers=providers)
    cascade_counts = score_form(record.form, truth, _scorable_for(record.vertical))
    return _Scored(
        vertical=entry.vertical,
        tier1_counts=tier1_counts,
        tier1_latency_ms=t1_latency,
        cascade_counts=cascade_counts,
        cascade_latency_ms=record.total_latency_ms,
    )


def run_eval(
    *,
    manifest_path: Path | str | None = None,
    db_path: Path | str,
    seed_path: Path | str = ALIAS_SEED_PATH,
) -> dict[str, list[tuple[int, float]]]:
    """Run the full progressive-batch sweep over the ``test`` split.

    Persists per-doc + per-batch rows into ``db_path`` (the same SQLite
    file the orchestrator uses; eval tables are harness-owned). Returns
    ``{"tier1": [(batch_id, f1), ...], "cascade": [...]}`` — ``"tier1"`` is
    the headline series, ``"cascade"`` the robustness series. ``EVAL_LIVE``
    is honored transparently by the providers.
    """
    from evals.manifest import MANIFEST_PATH

    seed_version, entries = load_manifest(manifest_path or MANIFEST_PATH)
    test_entries = [e for e in entries if e.split == "test"]
    n_batches = batch_count(load_seed(seed_path))
    git_sha = _git_sha()
    providers = build_cascade()
    conn = eval_store.connect(db_path)
    try:
        series: dict[str, list[tuple[int, float]]] = {"tier1": [], "cascade": []}
        for batch_id in range(1, n_batches + 1):
            with active_alias_batch(batch_id, seed_path):
                per_stage: dict[str, list[tuple[str, Counts, float]]] = {
                    "tier1": [],
                    "cascade": [],
                }
                for entry in test_entries:
                    scored = _score_entry(
                        entry,
                        validation_dir=CMS1500_VALIDATION_DIR,
                        providers=providers,
                        db_path=db_path,
                    )
                    if scored is None:
                        continue
                    for stage, counts, latency in (
                        ("tier1", scored.tier1_counts, scored.tier1_latency_ms),
                        ("cascade", scored.cascade_counts, scored.cascade_latency_ms),
                    ):
                        per_stage[stage].append((scored.vertical, counts, latency))
                        eval_store.record_doc_result(
                            conn,
                            doc_id=entry.doc_id,
                            batch_id=batch_id,
                            seed_version=seed_version,
                            stage=stage,
                            vertical=entry.vertical,
                            split=entry.split,
                            counts=counts,
                            latency_ms=latency,
                        )
                for stage in ("tier1", "cascade"):
                    metrics = aggregate(per_stage[stage])
                    eval_store.record_batch(
                        conn,
                        batch_id=batch_id,
                        seed_version=seed_version,
                        git_sha=git_sha,
                        stage=stage,
                        metrics=metrics,
                    )
                    series[stage].append((batch_id, metrics.f1))
                logger.info(
                    "batch %d/%d: tier1 F1=%.3f | cascade F1=%.3f | docs=%d",
                    batch_id,
                    n_batches,
                    series["tier1"][-1][1],
                    series["cascade"][-1][1],
                    len(per_stage["tier1"]),
                )
        return series
    finally:
        conn.close()


def write_fixtures_manifest(
    *,
    seed_version: str,
    entries: list[ManifestEntry],
    path: Path | str = FIXTURES_MANIFEST_PATH,
) -> None:
    """Thin manifest: which seed + models the cached fixtures pin to.

    Points at the existing ``tests/fixtures/eval-cache/`` slugs rather than
    duplicating a parallel fixture store (the approved layout decision).
    Deliberately carries **no** ``generated_at`` — the replay fixtures were
    generated in Phase 4, not per eval run, and this is a committed file:
    a per-run timestamp would churn the diff without adding provenance.
    """
    import json

    payload = {
        "seed_version": seed_version,
        "replay_cache": "tests/fixtures/eval-cache/",
        "providers": {
            "tier1_paddleocr_local": "PaddleOCR-VL-1.5",
            "tier2_qwen_7b_local": "qwen2.5vl:7b (Ollama)",
            "tier3_qwen_32b_local": "qwen2.5vl:32b Q4_K_M (Ollama registry)",
            "router_stage2_qwen_7b": "qwen2.5vl:7b (Ollama)",
        },
        "doc_ids": sorted(e.doc_id for e in entries),
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
