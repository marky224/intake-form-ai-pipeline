"""Measure the post-corrector: cascade-stage field-F1, with vs. without.

Reuses the **existing** harness metric verbatim
(``evals.metrics.score_form`` over ``evals.ground_truth``), so the number
is directly comparable to the Phase 6 cascade-stage F1 — the post-corrector
is scored exactly the way the cascade is.

**Two-stage framing is untouched.** This measures only the *cascade-stage*
form (``RunRecord.form``) before vs. after correction. The Tier-1-stage
headline series and the committed F1-over-time SVG are neither recomputed
nor relabeled — Phase 9 adds a post-cascade delta, it does not move the
portfolio artifact (memory ``project_phase6_two_stage_f1``).

On the committed corpus there is no trained adapter (GPU-box artifact), so
:func:`evaluate` honestly reports an **identity baseline: delta 0.000**.
That is the correct, publishable result at V1 committed scale — the
pipeline is proven end-to-end; a non-zero delta requires the
``FINETUNE_LIVE`` train run on the deferred local corpus.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class EvalResult(BaseModel):
    """The honest post-corrector report."""

    model_config = ConfigDict(extra="forbid")

    doc_count: int
    adapter_present: bool
    baseline_f1: float
    corrected_f1: float
    delta_f1: float
    note: str


def evaluate(
    *,
    manifest_path: Path | str | None = None,
    adapter_dir: Path | str | None = None,
) -> EvalResult:
    """Cascade-stage micro-F1 over the ``test`` split, baseline vs. corrected.

    Cached/$0/no-GPU by default (identity post-corrector). All first-party
    imports are local so the module stays cheap to import in CI.
    """
    from cascade.orchestrator import process_document
    from evals.ground_truth import FIELD_KIND, load_cms1500_ground_truth
    from evals.manifest import CMS1500_VALIDATION_DIR, MANIFEST_PATH, load_manifest
    from evals.metrics import Counts, score_form
    from finetune.correct import adapter_available, corrected_form
    from finetune.train import ADAPTER_DIR

    adapter_dir = Path(adapter_dir) if adapter_dir is not None else ADAPTER_DIR
    scorable = list(FIELD_KIND)
    _, entries = load_manifest(manifest_path or MANIFEST_PATH)

    base_total = Counts(0, 0, 0, 0)
    corr_total = Counts(0, 0, 0, 0)
    n = 0
    for entry in entries:
        if entry.split != "test" or entry.vertical != "healthcare":
            continue  # DocILE eval is local-only (CC-BY-NC-ND), skipped like the harness
        png_path = CMS1500_VALIDATION_DIR / f"{entry.doc_id}.png"
        sidecar = CMS1500_VALIDATION_DIR / f"{entry.doc_id}.json"
        if not png_path.is_file() or not sidecar.is_file():
            continue
        truth = load_cms1500_ground_truth(sidecar)
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            record = process_document(png_path.read_bytes(), doc_id=entry.doc_id, db_path=tmp.name)
        base_total = base_total + score_form(record.form, truth, scorable)
        corr_total = corr_total + score_form(
            corrected_form(record.form, scorable, adapter_dir), truth, scorable
        )
        n += 1

    present = adapter_available(adapter_dir)
    base_f1 = base_total.f1
    corr_f1 = corr_total.f1
    return EvalResult(
        doc_count=n,
        adapter_present=present,
        baseline_f1=round(base_f1, 4),
        corrected_f1=round(corr_f1, 4),
        delta_f1=round(corr_f1 - base_f1, 4),
        note=(
            "Real adapter applied — corrected vs. cascade-stage baseline."
            if present
            else "No trained adapter (GPU-box artifact): identity baseline, "
            "delta 0.000 is expected and honest at V1 committed scale. "
            "Run `FINETUNE_LIVE=true just finetune-train` on the deferred "
            "local corpus for a real number."
        ),
    )
