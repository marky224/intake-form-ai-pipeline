"""Tests for the Phase 7-V1 demo data layer (``demo/data.py``).

Deliberately does **not** import ``demo.app`` or ``streamlit`` — CI never
installs a browser/UI and the view layer carries no logic. Every case runs
the real cascade through the default cached-replay path: $0, deterministic,
no GPU. These assertions also pin the two carried-forward findings the demo
exists to present honestly (Phase 5 review-queue, Phase 6 two-stage F1).
"""

from __future__ import annotations

import sys

from cascade.orchestrator import RunRecord
from cascade.providers import _qwen_vl
from demo.data import (
    ESCALATION_GATE,
    ByStageSummary,
    CorrectionOutcome,
    CorrectionReplay,
    DemoDoc,
    DemoRun,
    FieldRow,
    TwoStageF1,
    by_stage_chart_svg,
    by_stage_summary,
    list_demo_docs,
    replay_review_queue_corrections,
    run_document,
    submit_correction,
    two_stage_f1,
)
from intake_schemas import HealthcareIntakeForm
from rag import aliases as rag_aliases


def test_no_streamlit_dependency_in_data_layer() -> None:
    """Importing the data layer must not drag in Streamlit (CI guard)."""
    assert "streamlit" not in sys.modules


def test_escalation_gate_matches_locked_cascade_value() -> None:
    """The demo reads the cascade's review gate, it does not redeclare it."""
    assert ESCALATION_GATE == 0.80


def test_list_demo_docs_returns_committed_cms1500_corpus() -> None:
    docs = list_demo_docs()
    # 92 = the patient-stratified test split of the 584-doc local corpus
    # (was the legacy 6-doc slice before the broad-corpus eval).
    assert len(docs) == 92
    for d in docs:
        assert isinstance(d, DemoDoc)
        assert d.png_path.is_file()
        assert d.sidecar_path.is_file()
        assert d.label  # patient-name label, never empty


def test_run_document_runs_the_full_cascade_cached() -> None:
    doc = list_demo_docs()[0]
    run = run_document(doc.doc_id)

    assert isinstance(run, DemoRun)
    assert isinstance(run.record, RunRecord)
    assert run.record.doc_id == doc.doc_id
    assert run.record.status in ("extracted", "review_queue")
    assert run.fields and all(isinstance(f, FieldRow) for f in run.fields)
    # Tier 2/3 prompted extraction populates well past Tier 1's 4-6 fields.
    assert sum(1 for f in run.fields if not f.blank) >= 10
    # in_review_queue must agree with the record's own status.
    assert run.in_review_queue == (run.record.status == "review_queue")


def test_unknown_doc_id_raises() -> None:
    try:
        run_document("not-a-real-doc-id")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for an uncommitted doc_id")


def _free_text_fields() -> set[str]:
    """String-typed promptable fields — the ones the old type heuristic
    scored 1.0 and therefore never parked."""
    return {
        name
        for name, (inner, _meta) in _qwen_vl._extractable_fields(HealthcareIntakeForm).items()
        if _qwen_vl._scalar_kind(inner) == "string_like"
    }


def test_second_read_disagreement_review_queue_reality() -> None:
    """CMS-1500s park for review on Tier 2 / Tier 3 disagreements, and the
    demo exposes the driving fields — the human-in-the-loop surface, not a
    hidden failure. Free-text fields park too: the pre-2026-10-16 type
    heuristic only ever parked dates and bools."""
    parked = []
    for doc in list_demo_docs():
        run = run_document(doc.doc_id)
        if run.in_review_queue:
            parked.append(run)
            assert run.review_drivers, "parked doc must expose its drivers"
            for name in run.review_drivers:
                row = next(f for f in run.fields if f.name == name)
                assert row.below_gate and not row.blank
    assert parked, "a second-read disagreement must reach review"
    free_text = _free_text_fields()
    assert any(
        set(run.review_drivers) & free_text for run in parked
    ), "a free-text disagreement must park, not only dates/bools"


def test_two_stage_f1_tier1_and_cascade_climb() -> None:
    """Tier-1 stage F1 climbs as the alias table fills (0.480 → 0.866 since
    2026-10-24). End-to-end cascade F1 climbs too (0.857 → 0.904): Tier 1's
    readings decide which fields Tier 3 reads. (Until 2026-10-24 the
    cascade was flat — the Phase 6 robustness stat.)"""
    ts = two_stage_f1()
    assert isinstance(ts, TwoStageF1)
    assert len(ts.tier1_series) == len(ts.cascade_series) >= 2

    # Headline: strictly climbs from the first batch, then asymptotes.
    assert ts.tier1_end > ts.tier1_start
    assert ts.tier1_start < 0.50 < ts.tier1_end

    # End-to-end: the cascade climbs with alias coverage too.
    assert ts.cascade_end - ts.cascade_start > 0.02
    assert 0.80 <= ts.cascade_mean <= 0.92


def test_by_stage_chart_svg_is_the_committed_artifact() -> None:
    svg = by_stage_chart_svg()
    assert svg.lstrip().startswith("<svg")
    # Funnel leads (top panel), F1 follows — both titles present.
    assert "Escalation funnel" in svg
    assert "F1 by cumulative cascade stage" in svg


def test_by_stage_summary_matches_measured_result_and_monotone_funnel() -> None:
    """The headline result, asserted exactly as measured, beside a
    cells-resolved funnel that is monotone to 100%. Deterministic from the
    committed fixtures. Until 2026-10-16 Tier 3 regressed (0.791 → 0.765);
    the Tier 3 second read changed the cascade, not the chart, so the F1
    triple is pinned rather than asserted to have any shape."""
    bs = by_stage_summary()
    assert isinstance(bs, ByStageSummary)

    f1s = [round(f, 3) for _, f in bs.stages]
    assert f1s == [0.866, 0.806, 0.904], f"by-stage F1 drifted: {f1s}"
    assert not bs.regresses

    cum = [c for _, c in bs.funnel_cumulative]
    assert cum == [250, 861, 989], f"funnel drifted: {cum}"
    assert cum == sorted(cum), "the cells-resolved funnel must be monotone"
    assert bs.populated == 989
    assert (bs.missed, bs.true_negative) == (92, 23)
    assert bs.total == 1104


# ---- Phase 8: correction feedback loop ----------------------------------


def test_replay_runs_cached_and_logs_corrections() -> None:
    """The seeded-reviewer replay runs end-to-end ($0, cached) and logs at
    least one correction — a second-read disagreement parks a doc."""
    rp = replay_review_queue_corrections()
    assert isinstance(rp, CorrectionReplay)
    assert rp.docs, "at least one CMS-1500 parks for review"
    assert rp.corrections_applied >= 1
    # Second-read disagreements put free-text fields in front of the
    # reviewer; the old type heuristic only ever parked dates and bools.
    corrected = {c.field_name for d in rp.docs for c in d.corrections}
    assert corrected & _free_text_fields(), f"only non-text fields corrected: {corrected}"
    for d in rp.docs:
        assert d.in_review_queue
        assert all(isinstance(c, CorrectionOutcome) for c in d.corrections)
    # No GPU / no ColQwen fixtures in CI → embedding refresh degrades to 0,
    # it must not raise.
    assert rp.embeddings_refreshed == 0
    assert rp.aliases_learned >= 0


def test_replay_never_touches_persistent_state(tmp_path, monkeypatch) -> None:
    """The demo must not write data/v1.db or the real alias overlay."""
    sentinel = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(rag_aliases, "OVERLAY_PATH", sentinel)
    replay_review_queue_corrections()
    assert not sentinel.exists()


def test_submit_correction_single_field() -> None:
    parked = next(
        (
            run_document(d.doc_id)
            for d in list_demo_docs()
            if run_document(d.doc_id).in_review_queue
        ),
        None,
    )
    assert parked is not None
    field = parked.review_drivers[0]
    out = submit_correction(parked.record.doc_id, field, "CORRECTED", label_phrasing=None)
    assert isinstance(out, CorrectionOutcome)
    assert out.field_name == field
    assert out.corrected_value == "CORRECTED"


def test_submit_correction_unknown_doc_raises() -> None:
    try:
        submit_correction("nope", "first_name", "x")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for an uncommitted doc_id")
