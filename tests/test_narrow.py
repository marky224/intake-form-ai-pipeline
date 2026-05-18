"""Tests for ``cascade.narrow`` — the synthetic narrowed sub-model.

The sub-model is the Protocol-preserving mechanism that lets Phase 5's
orchestrator escalate only the sub-threshold fields without changing the
frozen ``extract(png, form_cls)`` Protocol. These tests pin the two
load-bearing invariants from the module docstring:

1. ``_extractable_fields(sub) == requested`` — the downstream Qwen-VL prompt
   narrows because the sub-model's only ``FieldMeta``-annotated fields are
   the escalated ones (base-class person fields do NOT leak back in).
2. Each narrowed field keeps the *verbatim* ``Annotated[ExtractedField[X],
   FieldMeta(...)]`` hint, so the provider prompt text/type and the
   ``ExtractedField[X]`` inner-type validation behave identically to a
   full-form call.
"""

from __future__ import annotations

import pytest

from cascade.narrow import merge_fields, narrow_form_cls
from cascade.providers import _qwen_vl
from intake_schemas import (
    BusinessDocumentForm,
    ExtractedField,
    HealthcareIntakeForm,
    get_field_metadata,
)


def test_extractable_fields_equals_requested_subset():
    """The sub-model's promptable fields are exactly the requested names."""
    requested = ["first_name", "last_name", "date_of_birth"]
    sub = narrow_form_cls(HealthcareIntakeForm, requested)
    assert set(_qwen_vl._extractable_fields(sub)) == set(requested)


def test_base_class_fields_do_not_leak_back():
    """Narrowing on a BaseModel base (not form_cls) actually narrows.

    If the sub-model inherited HealthcareIntakeForm/IntakeFormBase, all the
    base person fields would re-enter ``_extractable_fields`` and re-widen
    the prompt — the bug Option-2 avoidance is guarding against.
    """
    full = set(_qwen_vl._extractable_fields(HealthcareIntakeForm))
    sub = narrow_form_cls(HealthcareIntakeForm, ["last_name"])
    sub_fields = set(_qwen_vl._extractable_fields(sub))
    assert sub_fields == {"last_name"}
    assert sub_fields < full  # strict subset — narrowing happened


def test_fieldmeta_preserved_verbatim():
    """The narrowed field's FieldMeta is the same description the provider
    prompts from — identical object semantics to the full form."""
    orig_meta = get_field_metadata(HealthcareIntakeForm)["first_name"]
    sub = narrow_form_cls(HealthcareIntakeForm, ["first_name"])
    _, sub_meta = _qwen_vl._extractable_fields(sub)["first_name"]
    assert sub_meta.description == orig_meta.description
    assert sub_meta.canonical_name == orig_meta.canonical_name


def test_inner_type_validation_preserved_via_parse_response():
    """A coerced inner type (date) still round-trips through the provider
    parser on the sub-model, proving ExtractedField[X] survived the copy."""
    sub = narrow_form_cls(HealthcareIntakeForm, ["date_of_birth"])
    raw = {"message": {"content": '{"date_of_birth": "1990-02-15"}'}}
    parsed = _qwen_vl.parse_response(raw, sub, tier=2, pipeline_version="test@narrow")
    ef = parsed.date_of_birth
    assert ef.value is not None
    assert str(ef.value) == "1990-02-15"
    # Coerced scalar → the locked 0.5 heuristic (not a model self-report).
    assert ef.confidence == _qwen_vl.FORMAT_COERCED_CONFIDENCE


def test_bad_value_demoted_to_confidently_blank_on_submodel():
    """An unparseable coerced value is demoted to confidently-blank, the
    same drop-rather-than-crash contract the full form has."""
    sub = narrow_form_cls(HealthcareIntakeForm, ["date_of_birth"])
    raw = {"message": {"content": '{"date_of_birth": "not-a-date"}'}}
    parsed = _qwen_vl.parse_response(raw, sub, tier=2, pipeline_version="test@narrow")
    assert parsed.date_of_birth.value is None
    assert parsed.date_of_birth.tier_used == 2  # attempted, blank


def test_empty_field_names_raises():
    with pytest.raises(ValueError, match="zero sub-threshold"):
        narrow_form_cls(HealthcareIntakeForm, [])


def test_unknown_field_name_raises():
    with pytest.raises(ValueError, match="not a field"):
        narrow_form_cls(HealthcareIntakeForm, ["definitely_not_a_field"])


def test_business_form_narrows_too():
    """Both V1 verticals flow through unchanged (KILE field set)."""
    some = list(_qwen_vl._extractable_fields(BusinessDocumentForm))[:3]
    sub = narrow_form_cls(BusinessDocumentForm, some)
    assert set(_qwen_vl._extractable_fields(sub)) == set(some)


def test_merge_fields_only_copies_named_and_preserves_others():
    """Higher tier writes back only the escalated names; a confident lower
    -tier value the higher tier never saw is left untouched."""
    running = HealthcareIntakeForm(
        metadata=_qwen_vl.stub_metadata(HealthcareIntakeForm, pipeline_version="t"),
        first_name=ExtractedField(value="Jane", confidence=0.99, tier_used=1),
        last_name=ExtractedField(value="LOWCONF", confidence=0.20, tier_used=1),
    )
    sub = narrow_form_cls(HealthcareIntakeForm, ["last_name"])
    higher = sub(
        metadata=running.metadata,
        last_name=ExtractedField(value="Fixed", confidence=1.0, tier_used=2),
    )
    merge_fields(running, higher, ["last_name"])

    assert running.last_name.value == "Fixed"
    assert running.last_name.tier_used == 2
    # first_name was never escalated → untouched, still the confident Tier 1.
    assert running.first_name.value == "Jane"
    assert running.first_name.tier_used == 1
