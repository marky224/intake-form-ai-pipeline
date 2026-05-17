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
from demo.data import (
    ESCALATION_GATE,
    DemoDoc,
    DemoRun,
    FieldRow,
    TwoStageF1,
    f1_chart_svg,
    list_demo_docs,
    run_document,
    two_stage_f1,
)


def test_no_streamlit_dependency_in_data_layer() -> None:
    """Importing the data layer must not drag in Streamlit (CI guard)."""
    assert "streamlit" not in sys.modules


def test_escalation_gate_matches_locked_cascade_value() -> None:
    """The demo reads the locked Tier-2→3 gate, it does not redeclare it."""
    assert ESCALATION_GATE == 0.80


def test_list_demo_docs_returns_committed_cms1500_corpus() -> None:
    docs = list_demo_docs()
    assert len(docs) == 6
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


def test_phase5_coerced_scalar_review_queue_reality() -> None:
    """At least one CMS-1500 necessarily parks for review on a coerced
    date/scalar (locked 0.5 heuristic < 0.80 gate). When it does, the demo
    must expose the driving fields — that is the human-in-the-loop surface,
    not a hidden failure."""
    parked = []
    for doc in list_demo_docs():
        run = run_document(doc.doc_id)
        if run.in_review_queue:
            parked.append(run)
            assert run.review_drivers, "parked doc must expose its drivers"
            for name in run.review_drivers:
                row = next(f for f in run.fields if f.name == name)
                assert row.below_gate and not row.blank
    assert parked, "Phase 5 reality: a coerced-scalar doc must reach review"


def test_two_stage_f1_headline_climbs_cascade_stays_flat() -> None:
    """Phase 6 finding, asserted: Tier-1 stage F1 climbs as the alias table
    fills; end-to-end cascade F1 is flat ≈0.78 (the robustness stat)."""
    ts = two_stage_f1()
    assert isinstance(ts, TwoStageF1)
    assert len(ts.tier1_series) == len(ts.cascade_series) >= 2

    # Headline: strictly climbs from the first batch, then asymptotes.
    assert ts.tier1_end > ts.tier1_start
    assert ts.tier1_start < 0.30 < ts.tier1_end

    # Robustness: cascade barely moves across the whole sweep.
    cascade_vals = [f1 for _, f1 in ts.cascade_series]
    assert max(cascade_vals) - min(cascade_vals) < 0.02
    assert 0.75 <= ts.cascade_mean <= 0.82


def test_f1_chart_svg_is_the_committed_artifact() -> None:
    svg = f1_chart_svg()
    assert svg.lstrip().startswith("<svg")
    assert "F1 over progressive alias-table batches" in svg
