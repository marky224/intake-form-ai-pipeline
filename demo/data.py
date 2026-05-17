"""Phase 7-V1 local demo — testable data layer (NO Streamlit import).

``demo/app.py`` is the Streamlit view; this module is the core it calls, and
the only half CI imports and tests. Everything here runs the real V1 cascade
through the **default cached-replay path**: providers short-circuit to the
committed ``tests/fixtures/eval-cache/`` fixtures when ``EVAL_LIVE`` is unset,
so ``just demo`` runs end-to-end for **$0, deterministically, with nothing on
the GPU** (no Ollama, no Paddle process).

Two design points worth stating up front, both carried forward from earlier
phases and surfaced honestly by the demo rather than hidden:

1. **The F1-over-time story is two-stage** (Phase 6, memory
   ``project_phase6_two_stage_f1``). The *headline* curve is **Tier-1-stage**
   F1 — it climbs as the progressive alias table fills (0.222 → 0.322 then
   asymptotes on the 6 CMS-1500). End-to-end **cascade** F1 is **flat
   ≈0.78**: strong Tier 2/3 escalation compensates, so the alias table barely
   moves the merged result. The flat number is the *robustness* stat, not a
   failed climb — :func:`two_stage_f1` returns both series and the demo
   presents both.

2. **Coerced scalars route to human review** (Phase 5, memory
   ``project_phase5_coerced_review_queue``). The locked Tier-2/3 confidence
   heuristic scores a coerced date/int/float/bool at 0.5, below the locked
   0.80 escalation gate, so any CMS-1500 with such a field necessarily ends
   in ``review_queue`` after Tier 3. That is the *intended* human-in-the-loop
   surface — :func:`run_document` exposes it as a feature to show off, not a
   bug to hide.

The demo never writes the real ``data/v1.db``: each cascade run and each eval
sweep uses a throwaway temp database, so the demo is idempotent and safe to
re-run from the UI.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cascade.orchestrator import (
    GATE_TIER1_TO_TIER2,
    GATE_TIER2_TO_TIER3,
    RunRecord,
    process_document,
)
from cascade.store import RUN_STATUS_REVIEW, connect
from evals.ground_truth import load_cms1500_ground_truth
from evals.harness import FIXTURES_MANIFEST_PATH, run_eval
from evals.manifest import CMS1500_VALIDATION_DIR
from intake_schemas import ExtractedField
from rag.aliases import temporary_overlay
from rag.corrections import (
    CorrectionOutcome,
    count_corrections,
    humanize_field_label,
    record_correction,
    refresh_embedding,
)
from rag.retrieve import Neighbor, top_k
from rag.store import load_corpus, load_embedding

#: Repo root, resolved from this file so paths work regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The committed portfolio artifact. Phase 6 owns its regeneration
#: (``just chart``); the demo only *surfaces* it — it never rewrites it.
F1_CHART_SVG_PATH = _REPO_ROOT / "docs" / "assets" / "f1-over-time.svg"

#: The locked escalation gate the per-field panel highlights against. A
#: populated value below this never reaches the final form unchallenged — it
#: escalates, and if it survives Tier 3 still below gate the doc parks for
#: review. Imported, not redeclared, so the demo can't drift from the cascade.
ESCALATION_GATE = GATE_TIER2_TO_TIER3


@dataclass(frozen=True)
class DemoDoc:
    """One selectable CMS-1500 fixture (committed, CI-safe, cached, $0)."""

    doc_id: str
    png_path: Path
    sidecar_path: Path
    #: Ground-truth patient name from the sidecar — a human-readable label
    #: for the picker (the doc_id alone is an opaque UUID+sha).
    label: str


@dataclass(frozen=True)
class FieldRow:
    """One extracted field, flattened for the per-field panel."""

    name: str
    value: str
    confidence: float
    tier_used: str | None
    #: True iff a populated value landed below the escalation gate — the
    #: locked coerced-scalar (0.5) case is the common driver here.
    below_gate: bool
    #: True iff the field was never populated by any tier (confident blank or
    #: genuinely absent — ``tier_used`` set, ``value`` empty).
    blank: bool


@dataclass(frozen=True)
class DemoRun:
    """Everything the UI renders for one document, from one cascade pass."""

    record: RunRecord
    fields: list[FieldRow]
    #: ``True`` when the cascade parked this doc for human review (Tier 3
    #: exhausted with fields still under gate — the Phase 5 reality).
    in_review_queue: bool
    #: Field names that drove the review parking (populated-but-under-gate).
    review_drivers: list[str]


@dataclass(frozen=True)
class TwoStageF1:
    """The honest two-stage F1 story (Phase 6 finding)."""

    #: Headline: Tier-1-stage F1 per progressive alias batch — it climbs.
    tier1_series: list[tuple[int, float]]
    #: Robustness: end-to-end cascade F1 per batch — it stays flat.
    cascade_series: list[tuple[int, float]]

    @property
    def tier1_start(self) -> float:
        return self.tier1_series[0][1]

    @property
    def tier1_end(self) -> float:
        return self.tier1_series[-1][1]

    @property
    def cascade_mean(self) -> float:
        """The flat ≈0.78 robustness number (mean across batches)."""
        vals = [f1 for _, f1 in self.cascade_series]
        return sum(vals) / len(vals)


def _sidecar_label(sidecar_path: Path) -> str:
    """Patient-name ground truth from a CMS-1500 sidecar, for the picker."""
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        for field in data.get("fields", []):
            if field.get("name") == "patient_name" and field.get("value"):
                return str(field["value"])
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return sidecar_path.stem


def list_demo_docs() -> list[DemoDoc]:
    """The committed CMS-1500 corpus, in fixtures-manifest order.

    Intersects the Phase 6 fixtures manifest's pinned ``doc_ids`` with the
    PNG/sidecar pairs actually present on disk, so a partial checkout (or a
    future trimmed manifest) degrades cleanly instead of raising.
    """
    manifest = json.loads(FIXTURES_MANIFEST_PATH.read_text(encoding="utf-8"))
    docs: list[DemoDoc] = []
    for doc_id in manifest.get("doc_ids", []):
        png = CMS1500_VALIDATION_DIR / f"{doc_id}.png"
        sidecar = CMS1500_VALIDATION_DIR / f"{doc_id}.json"
        if png.is_file() and sidecar.is_file():
            docs.append(
                DemoDoc(
                    doc_id=doc_id,
                    png_path=png,
                    sidecar_path=sidecar,
                    label=_sidecar_label(sidecar),
                )
            )
    return docs


def _field_rows(form: Any) -> list[FieldRow]:
    """Flatten every ``ExtractedField`` on ``form`` for the per-field panel.

    Iterates the model's declared fields and keeps the ones whose live value
    is an ``ExtractedField`` — ``metadata`` (a plain sub-model) is naturally
    excluded, and the walk stays vertical-agnostic (healthcare or business).
    """
    rows: list[FieldRow] = []
    for name in type(form).model_fields:
        ef = getattr(form, name, None)
        if not isinstance(ef, ExtractedField):
            continue
        blank = ef.value is None
        below_gate = (not blank) and ef.confidence < ESCALATION_GATE
        rows.append(
            FieldRow(
                name=name,
                value="" if blank else str(ef.value),
                confidence=ef.confidence,
                tier_used=None if ef.tier_used is None else str(ef.tier_used),
                below_gate=below_gate,
                blank=blank,
            )
        )
    return rows


def run_document(doc_id: str) -> DemoRun:
    """Run one committed CMS-1500 through the full cascade (cached, $0).

    Uses a throwaway temp DB so the demo never mutates the real
    ``data/v1.db`` and is safe to re-run. The returned ``RunRecord`` is the
    cascade's own output — the demo derives every panel from it and adds no
    store columns.
    """
    docs = {d.doc_id: d for d in list_demo_docs()}
    if doc_id not in docs:
        raise KeyError(f"{doc_id!r} is not a committed demo document")
    png = docs[doc_id].png_path.read_bytes()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        record = process_document(png, doc_id=doc_id, db_path=tmp.name)

    fields = _field_rows(record.form)
    drivers = [r.name for r in fields if r.below_gate]
    return DemoRun(
        record=record,
        fields=fields,
        in_review_queue=record.status == RUN_STATUS_REVIEW,
        review_drivers=drivers,
    )


def two_stage_f1() -> TwoStageF1:
    """Compute both F1 series the honest way — the same sweep ``just eval``
    runs, cached, into a throwaway DB (mirrors ``python -m evals chart``).

    Returns the Tier-1-stage headline series (climbs) and the end-to-end
    cascade robustness series (flat ≈0.78). The committed SVG plots the
    former; the demo shows the latter alongside it so the flat number is
    never relabeled as a climbing curve.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=True) as tmp:
        series = run_eval(db_path=tmp.name)
    return TwoStageF1(
        tier1_series=[(int(b), float(f)) for b, f in series["tier1"]],
        cascade_series=[(int(b), float(f)) for b, f in series["cascade"]],
    )


def f1_chart_svg() -> str:
    """The committed F1-over-time SVG markup (Tier-1 headline curve).

    Surfaced verbatim — the demo does not regenerate the portfolio artifact.
    """
    return F1_CHART_SVG_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True)
class DocCorrections:
    """The Phase 8 correction-loop outcome for one parked document."""

    doc_id: str
    label: str
    in_review_queue: bool
    corrections: list[CorrectionOutcome]
    #: Nearest other corrected documents by ColQwen MaxSim. Empty unless
    #: ColQwen ``.npy`` fixtures are present (no GPU in CI → degrades).
    neighbors: list[Neighbor]


@dataclass(frozen=True)
class CorrectionReplay:
    """A full seeded-reviewer replay over the parked demo documents.

    The seeded-replay analogue of a reviewer working the queue: each parked
    CMS-1500's below-gate fields are "corrected" to their sidecar ground
    truth, the humanized field label is offered as the on-form phrasing
    (learned only when not already a recognized alias — usually a no-op for
    canonical-named fields, which is honest, not inflated), and the doc is
    re-embedded into the retrieval corpus. Runs cached/$0 into a throwaway
    DB **and a throwaway alias overlay** — the real ``data/v1.db`` and
    ``data/corrections_aliases.json`` are never touched.
    """

    docs: list[DocCorrections]
    corrections_applied: int
    aliases_learned: int
    embeddings_refreshed: int


def _below_gate_drivers(record: RunRecord) -> list[FieldRow]:
    """Populated-but-under-gate fields — the reviewer's worklist."""
    return [r for r in _field_rows(record.form) if r.below_gate]


def replay_review_queue_corrections() -> CorrectionReplay:
    """Seeded-reviewer replay over every parked demo document (cached, $0).

    Mirrors ``run_document``'s isolation guarantees: a throwaway temp DB and
    a throwaway alias overlay, so the demo is idempotent and never mutates
    persistent state. CI exercises this end-to-end for $0 (ColQwen
    embedding degrades to a no-op without a GPU/fixture).
    """
    demo_docs = list_demo_docs()
    corrections_applied = aliases_learned = embeddings_refreshed = 0
    parked: list[tuple[DemoDoc, RunRecord, list[FieldRow]]] = []

    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "replay.db")
        overlay_path = Path(td) / "corrections_aliases.json"
        with temporary_overlay(overlay_path):
            for d in demo_docs:
                png = d.png_path.read_bytes()
                record = process_document(png, doc_id=d.doc_id, db_path=db_path)
                drivers = _below_gate_drivers(record)
                if record.status == RUN_STATUS_REVIEW and drivers:
                    parked.append((d, record, drivers))

            per_doc: dict[str, list[CorrectionOutcome]] = {}
            conn = connect(db_path)
            try:
                for d, record, drivers in parked:
                    truth = load_cms1500_ground_truth(d.sidecar_path)
                    outcomes: list[CorrectionOutcome] = []
                    for r in drivers:
                        corrected = truth.get(r.name)
                        if not corrected:
                            continue
                        outcome = record_correction(
                            conn,
                            doc_id=d.doc_id,
                            field_name=r.name,
                            original_value=r.value or None,
                            corrected_value=corrected,
                            vertical=record.vertical,
                            tier_that_produced_original=r.tier_used,
                            label_phrasing=humanize_field_label(r.name),
                        )
                        outcomes.append(outcome)
                        corrections_applied += 1
                        if outcome.alias_learned:
                            aliases_learned += 1
                    per_doc[d.doc_id] = outcomes
                    if refresh_embedding(conn, d.doc_id, d.png_path.read_bytes()):
                        embeddings_refreshed += 1

                docs: list[DocCorrections] = []
                for d, _record, _drivers in parked:
                    query = load_embedding(conn, d.doc_id)
                    neighbors = (
                        top_k(query, load_corpus(conn, exclude=[d.doc_id]))
                        if query is not None
                        else []
                    )
                    docs.append(
                        DocCorrections(
                            doc_id=d.doc_id,
                            label=d.label,
                            in_review_queue=True,
                            corrections=per_doc.get(d.doc_id, []),
                            neighbors=neighbors,
                        )
                    )
            finally:
                conn.close()

    return CorrectionReplay(
        docs=docs,
        corrections_applied=corrections_applied,
        aliases_learned=aliases_learned,
        embeddings_refreshed=embeddings_refreshed,
    )


def submit_correction(
    doc_id: str,
    field_name: str,
    corrected_value: str,
    label_phrasing: str | None = None,
) -> CorrectionOutcome:
    """Record one interactive reviewer correction (the demo button).

    Re-runs the document cached to recover the original value/tier and a
    ``runs`` row for the FK, then logs the correction into a throwaway DB +
    overlay. ``label_phrasing`` is the on-form label the reviewer saw the
    cascade miss — supplying it closes the alias half of the loop live
    (``alias_learned=True`` when it is genuinely new vocabulary).
    """
    docs = {d.doc_id: d for d in list_demo_docs()}
    if doc_id not in docs:
        raise KeyError(f"{doc_id!r} is not a committed demo document")
    d = docs[doc_id]
    png = d.png_path.read_bytes()

    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "correct.db")
        overlay_path = Path(td) / "corrections_aliases.json"
        with temporary_overlay(overlay_path):
            record = process_document(png, doc_id=doc_id, db_path=db_path)
            row = next((r for r in _field_rows(record.form) if r.name == field_name), None)
            conn = connect(db_path)
            try:
                return record_correction(
                    conn,
                    doc_id=doc_id,
                    field_name=field_name,
                    original_value=(row.value or None) if row else None,
                    corrected_value=corrected_value,
                    vertical=record.vertical,
                    tier_that_produced_original=(row.tier_used if row else None),
                    label_phrasing=label_phrasing,
                )
            finally:
                conn.close()


def corrections_logged(replay: CorrectionReplay) -> int:
    """Total corrections in a replay (the demo's headline Phase 8 stat)."""
    return replay.corrections_applied


# Re-exported so the view layer states the gates without re-deriving them.
__all__ = [
    "DocCorrections",
    "CorrectionReplay",
    "CorrectionOutcome",
    "replay_review_queue_corrections",
    "submit_correction",
    "corrections_logged",
    "count_corrections",
    "DemoDoc",
    "DemoRun",
    "FieldRow",
    "TwoStageF1",
    "ESCALATION_GATE",
    "GATE_TIER1_TO_TIER2",
    "GATE_TIER2_TO_TIER3",
    "F1_CHART_SVG_PATH",
    "list_demo_docs",
    "run_document",
    "two_stage_f1",
    "f1_chart_svg",
]
