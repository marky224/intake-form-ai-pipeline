"""Tests for ``cascade.providers._qwen_vl`` — the shared Qwen-VL core.

Tier 2 (7B) and Tier 3 (32B) are the same model family and share all
prompt-building, response-schema, JSON-parsing, and confidence-heuristic
logic. Those tests live here once (moved out of
``test_tier2_qwen_7b_local`` when the shared core was extracted in PR
(d-V1)); the per-tier test files now only cover each tier's constants +
thin class + ``_invoke_model`` / ``_load_ollama_client`` seams.

``parse_response`` is parameterized over ``tier`` — every parse test runs
against both the integer Tier 2 (``2``) and the lettered Tier 3 (``"3a"``)
to prove the ``str`` tier member flows through ``ExtractedField.tier_used``
and the confidently-blank contract identically to the integer tiers
(``compute_form_confidence`` only ever does ``tier_used is (not) None``).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal, get_type_hints

import pytest

from cascade.providers._qwen_vl import (
    CLEAN_VALUE_CONFIDENCE,
    FORMAT_COERCED_CONFIDENCE,
    _extractable_fields,
    _inner_type,
    _scalar_kind,
    build_extraction_prompt,
    build_response_schema,
    extract_json_object,
    parse_response,
)
from intake_schemas import (
    BusinessDocumentForm,
    HealthcareIntakeForm,
    SignatureCapture,
)

#: Every parse test runs against both tiers — int (Tier 2) and str (Tier 3).
#: ``(tier, pipeline_version)`` pairs mirror what each tier module passes.
_TIER_PARAMS = [
    pytest.param(2, "tier2-qwen2.5-vl-7b@2", id="tier2-int"),
    pytest.param("3a", "tier3-qwen2.5-vl-32b@3a", id="tier3-str"),
]


def _ollama_raw(payload: dict | str) -> dict:
    """Build an Ollama ``chat`` response dict (the cached-fixture shape).

    ``payload`` is either the dict the model 'returned' (serialized to the
    message content as JSON) or a raw content string for malformed-response
    tests. Model tag is generic — the shared core is tier-agnostic.
    """
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "model": "qwen2.5vl",
        "created_at": "2026-05-16T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 1234567,
    }


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------


def test_inner_type_unwraps_pydantic_generic():
    """ExtractedField[str] is a Pydantic generic — get_args() is empty, so
    _inner_type must read __pydantic_generic_metadata__."""
    hints = get_type_hints(HealthcareIntakeForm, include_extras=True)
    assert _inner_type(hints["first_name"]) is str
    assert _inner_type(hints["date_of_birth"]) is date


def test_scalar_kind_classification():
    assert _scalar_kind(str) == "string_like"
    assert _scalar_kind(date) == "coerced"
    assert _scalar_kind(bool) == "coerced"
    assert _scalar_kind(float) == "coerced"
    assert _scalar_kind(Literal["M", "F", "U"]) == "string_like"
    # Non-promptable: nested models / collections fall through to None.
    assert _scalar_kind(SignatureCapture) is None
    assert _scalar_kind(list[str]) is None


def test_extractable_fields_both_forms_nonempty_and_typed():
    hf = _extractable_fields(HealthcareIntakeForm)
    bf = _extractable_fields(BusinessDocumentForm)
    assert len(hf) > 20 and len(bf) > 20
    # The schema-driven path needs zero per-vertical wiring — both work.
    assert "first_name" in hf and _scalar_kind(hf["first_name"][0]) == "string_like"
    assert "date_of_birth" in hf and _scalar_kind(hf["date_of_birth"][0]) == "coerced"
    assert "vendor_name" in bf
    # Non-scalar fields are excluded (signature is a SignatureCapture model).
    assert "signature" not in hf


# ---------------------------------------------------------------------------
# Prompt + response schema
# ---------------------------------------------------------------------------


def test_build_prompt_lists_every_extractable_field_and_is_deterministic():
    p1 = build_extraction_prompt(HealthcareIntakeForm)
    p2 = build_extraction_prompt(HealthcareIntakeForm)
    assert p1 == p2  # pure function of form_cls
    for name in _extractable_fields(HealthcareIntakeForm):
        assert f'"{name}"' in p1
    assert "JSON object" in p1
    assert "null" in p1  # absent-field instruction present


def test_build_prompt_works_for_business_form_without_alias_machinery():
    p = build_extraction_prompt(BusinessDocumentForm)
    assert '"vendor_name"' in p
    assert '"line_items"' not in p  # non-scalar excluded


def test_build_response_schema_is_constrained_and_nullable():
    schema = build_response_schema(HealthcareIntakeForm)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    extractable = _extractable_fields(HealthcareIntakeForm)
    assert set(schema["properties"]) == set(extractable)
    assert set(schema["required"]) == set(extractable)
    # Every property is nullable so the model can confidently decline.
    assert schema["properties"]["first_name"] == {"type": ["string", "null"]}
    # date inner type still serializes as a (ISO) string at the wire level.
    assert schema["properties"]["date_of_birth"] == {"type": ["string", "null"]}


# ---------------------------------------------------------------------------
# extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_object_clean():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_strips_markdown_fence():
    assert extract_json_object('```json\n{"a": "b"}\n```') == {"a": "b"}


def test_extract_json_object_scrapes_first_balanced_span():
    assert extract_json_object('noise {"a": {"b": 2}} trailing') == {"a": {"b": 2}}


def test_extract_json_object_malformed_returns_empty():
    assert extract_json_object("not json at all") == {}
    assert extract_json_object("") == {}
    assert extract_json_object("[1, 2, 3]") == {}  # top-level array, not object


# ---------------------------------------------------------------------------
# parse_response — run against BOTH tiers (int 2 + str "3a")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_string_field_clean_confidence_1(tier, pipeline_version):
    f = parse_response(
        _ollama_raw({"first_name": "Jane"}),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.first_name.value == "Jane"
    assert f.first_name.confidence == CLEAN_VALUE_CONFIDENCE
    assert f.first_name.tier_used == tier
    assert f.first_name.bounding_box is None  # locked decision 2
    assert f.metadata.pipeline_version == pipeline_version


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_strips_whitespace(tier, pipeline_version):
    f = parse_response(
        _ollama_raw({"last_name": "  Doe  "}),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.last_name.value == "Doe"


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_coerced_date_confidence_half(tier, pipeline_version):
    f = parse_response(
        _ollama_raw({"date_of_birth": "1985-03-12"}),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.date_of_birth.value == date(1985, 3, 12)
    assert f.date_of_birth.confidence == FORMAT_COERCED_CONFIDENCE
    assert f.date_of_birth.tier_used == tier


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_null_is_confidently_blank(tier, pipeline_version):
    """A prompted field the model returned null for is attempted-but-blank:
    value=None, tier_used=<tier> (NOT tier_used=None) — the signal
    compute_form_confidence keys off, identical for int and str tiers."""
    f = parse_response(
        _ollama_raw({"first_name": None}),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.first_name.value is None
    assert f.first_name.tier_used == tier
    assert f.first_name.tier_used is not None  # confidently-blank, not unattempted


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_non_scalar_field_stays_unattempted(tier, pipeline_version):
    """signature (SignatureCapture) is never prompted → tier_used stays None."""
    f = parse_response(
        _ollama_raw({"first_name": "Jane"}),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.signature.tier_used is None


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_bad_value_demoted_to_confidently_blank(tier, pipeline_version):
    """Unparseable date / bad Literal was attempted → confidently-blank,
    not silently dropped to unattempted (prompted-VL contract, differs
    from Tier 1)."""
    raw = _ollama_raw({"date_of_birth": "NOT-A-DATE", "sex": "Martian", "first_name": "Bob"})
    f = parse_response(raw, HealthcareIntakeForm, tier=tier, pipeline_version=pipeline_version)
    assert f.date_of_birth.value is None and f.date_of_birth.tier_used == tier
    assert f.sex.value is None and f.sex.tier_used == tier
    assert f.first_name.value == "Bob"  # good field survives the rebuild


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_ignores_extra_keys(tier, pipeline_version):
    f = parse_response(
        _ollama_raw({"first_name": "Jane", "not_a_field": "x"}),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.first_name.value == "Jane"


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_malformed_json_yields_all_blank_no_crash(tier, pipeline_version):
    f = parse_response(
        _ollama_raw("garbage{not json"),
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.first_name.value is None
    assert f.first_name.tier_used == tier  # every prompted field stamped


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_business_form(tier, pipeline_version):
    f = parse_response(
        _ollama_raw({"vendor_name": "Acme Corp"}),
        BusinessDocumentForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.vendor_name.value == "Acme Corp"
    assert f.vendor_name.tier_used == tier


@pytest.mark.parametrize("tier,pipeline_version", _TIER_PARAMS)
def test_parse_response_handles_legacy_dict_message_shape(tier, pipeline_version):
    """Defensive: a response missing message.content yields an all-blank form."""
    f = parse_response(
        {"model": "qwen2.5vl", "done": True},
        HealthcareIntakeForm,
        tier=tier,
        pipeline_version=pipeline_version,
    )
    assert f.first_name.value is None
    assert f.first_name.tier_used == tier
