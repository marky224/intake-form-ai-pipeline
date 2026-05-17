"""F1 TP/FP/FN semantics + latency percentiles (eval-methodology.md)."""

from __future__ import annotations

from datetime import date

from cascade.providers.tier1_paddleocr_local import _stub_metadata
from evals.metrics import Counts, _percentile, aggregate, score_form
from intake_schemas import ExtractedField, HealthcareIntakeForm


def _form(**fields) -> HealthcareIntakeForm:
    f = HealthcareIntakeForm(metadata=_stub_metadata(HealthcareIntakeForm))
    for name, ef in fields.items():
        setattr(f, name, ef)
    return f


def test_counts_arithmetic_and_derived():
    c = Counts(8, 2, 2) + Counts(0, 0, 0)
    assert (c.tp, c.fp, c.fn) == (8, 2, 2)
    assert c.precision == 0.8
    assert c.recall == 0.8
    assert round(c.f1, 3) == 0.8
    # Degenerate all-true-negative batch: no tp/fp/fn → p=r=1.0 by the
    # zero-denominator convention → f1 1.0. Never hit in an eval batch
    # (truth always present), asserted only to pin the convention.
    assert Counts().f1 == 1.0


def test_true_positive_match():
    form = _form(first_name=ExtractedField(value="Jane", confidence=0.9, tier_used=1))
    c = score_form(form, {"first_name": "jane"}, ["first_name"])
    assert (c.tp, c.fp, c.fn) == (1, 0, 0)


def test_wrong_populated_value_is_both_fp_and_fn():
    form = _form(first_name=ExtractedField(value="John", confidence=0.9, tier_used=1))
    c = score_form(form, {"first_name": "jane"}, ["first_name"])
    assert (c.tp, c.fp, c.fn) == (0, 1, 1)


def test_ghost_value_no_ground_truth_is_fp_only():
    form = _form(first_name=ExtractedField(value="John", confidence=0.9, tier_used=1))
    c = score_form(form, {}, ["first_name"])
    assert (c.tp, c.fp, c.fn) == (0, 1, 0)


def test_missing_extraction_with_truth_is_fn():
    form = _form()  # first_name unattempted (tier_used None, value None)
    c = score_form(form, {"first_name": "jane"}, ["first_name"])
    assert (c.tp, c.fp, c.fn) == (0, 0, 1)


def test_confidently_blank_excluded_from_pr():
    # value None but tier_used set → affirmative absence → excluded.
    form = _form(first_name=ExtractedField(value=None, confidence=0.95, tier_used=2))
    c = score_form(form, {"first_name": "jane"}, ["first_name"])
    assert (c.tp, c.fp, c.fn, c.blank_excluded) == (0, 0, 0, 1)


def test_true_negative_uncounted():
    form = _form()
    c = score_form(form, {}, ["first_name"])
    assert (c.tp, c.fp, c.fn, c.blank_excluded) == (0, 0, 0, 0)


def test_date_field_canonicalized_both_sides():
    form = _form(date_of_birth=ExtractedField(value=date(1996, 1, 26), confidence=0.9, tier_used=1))
    c = score_form(form, {"date_of_birth": "1996-01-26"}, ["date_of_birth"])
    assert c.tp == 1


def test_percentiles_nearest_rank():
    assert _percentile([], 50) == 0.0
    assert _percentile([10, 20, 30, 40], 50) == 20
    assert _percentile([10, 20, 30, 40], 99) == 40


def test_aggregate_micro_average_and_per_vertical():
    per_doc = [
        ("healthcare", Counts(5, 1, 1), 10.0),
        ("healthcare", Counts(3, 0, 2), 30.0),
    ]
    m = aggregate(per_doc)
    assert m.counts == Counts(8, 1, 3)
    assert m.doc_count == 2
    assert m.cost_per_doc_usd == 0.0
    assert m.latency_p50_ms == 10.0
    assert "healthcare" in m.per_vertical
    assert m.per_vertical["healthcare"] == Counts(8, 1, 3)
