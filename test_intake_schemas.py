"""
Smoke tests verifying the contract claims made in RATIONALE.md, plus tests
for the four gap fixes:
  - PageMetadata model
  - BoundingBox model
  - DataClass enum (replaces is_pii/is_phi booleans)
  - compute_form_confidence module-level function

Run:
    python -m pytest test_intake_schemas.py -v
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from intake_schemas import (
    BoundingBox,
    DataClass,
    ExtractedField,
    FormMetadata,
    HealthcareIntakeForm,
    HRIntakeForm,
    InsuranceIntakeForm,
    IntakeFormBase,
    LossEvent,
    PageMetadata,
    TierAttempt,
    compute_form_confidence,
    get_field_metadata,
    is_baa_required,
)

# ---------------------------------------------------------------------------
# ExtractedField wrapper contracts
# ---------------------------------------------------------------------------


def test_extracted_field_defaults_are_unset():
    """A fresh ExtractedField means 'not yet attempted', distinguishable from 'attempted, found nothing'."""
    f: ExtractedField = ExtractedField()
    assert f.value is None
    assert f.confidence == 0.0
    assert f.tier_used is None
    assert f.escalation_history == []
    assert f.bounding_box is None


def test_extracted_field_confidence_bounds():
    with pytest.raises(ValidationError):
        ExtractedField(value="x", confidence=1.5, tier_used=1)
    with pytest.raises(ValidationError):
        ExtractedField(value="x", confidence=-0.1, tier_used=1)


def test_tier_attempt_is_frozen():
    a = TierAttempt(tier=1, confidence=0.5)
    with pytest.raises(ValidationError):
        a.confidence = 0.9  # type: ignore[misc]


def test_escalation_history_appends():
    """Escalation history records each tier attempt in order."""
    f = ExtractedField[str](
        value="John",
        confidence=0.92,
        tier_used="3a",
        escalation_history=[
            TierAttempt(tier=1, confidence=0.41, reason_escalated="low_confidence"),
            TierAttempt(tier=2, confidence=0.58, reason_escalated="low_confidence"),
        ],
    )
    assert f.tier_used == "3a"
    assert [a.tier for a in f.escalation_history] == [1, 2]


# ---------------------------------------------------------------------------
# Gap 2: BoundingBox model
# ---------------------------------------------------------------------------


def test_bounding_box_requires_page_number():
    """page_number is required and must be >= 1 (1-indexed)."""
    bb = BoundingBox(page_number=1, x1=10.0, y1=20.0, x2=110.0, y2=40.0)
    assert bb.page_number == 1
    assert bb.x1 == 10.0


def test_bounding_box_page_number_must_be_positive():
    with pytest.raises(ValidationError):
        BoundingBox(page_number=0, x1=10.0, y1=20.0, x2=110.0, y2=40.0)


def test_bounding_box_is_frozen():
    """Bounding boxes shouldn't mutate after extraction."""
    bb = BoundingBox(page_number=1, x1=10.0, y1=20.0, x2=110.0, y2=40.0)
    with pytest.raises(ValidationError):
        bb.x1 = 99.0  # type: ignore[misc]


def test_extracted_field_with_bounding_box():
    """ExtractedField holds a typed BoundingBox, page_number lives inside it."""
    f = ExtractedField[str](
        value="Jane",
        confidence=0.97,
        tier_used=1,
        bounding_box=BoundingBox(page_number=2, x1=100.0, y1=200.0, x2=300.0, y2=240.0),
    )
    assert f.bounding_box is not None
    assert f.bounding_box.page_number == 2
    assert f.bounding_box.x1 == 100.0


# ---------------------------------------------------------------------------
# Gap 1: PageMetadata model
# ---------------------------------------------------------------------------


def test_page_metadata_defaults():
    p = PageMetadata(
        page_number=1,
        page_image_uri="s3://bucket/forms/abc/page-1.png",
    )
    assert p.page_number == 1
    assert p.page_confidence == 0.0
    assert p.tier_used_for_page is None
    assert p.page_status == "extracted"
    assert p.rotation_corrected is None


def test_page_metadata_skipped_blank():
    """Blank pages are tracked so routing layer doesn't escalate them."""
    p = PageMetadata(
        page_number=3,
        page_image_uri="s3://bucket/forms/abc/page-3.png",
        page_status="skipped_blank",
        page_confidence=0.99,
    )
    assert p.page_status == "skipped_blank"


def test_form_metadata_pages_default_empty():
    """Single-page forms can omit the pages list entirely."""
    fm = FormMetadata(
        form_type="W-4_2026",
        source_document_id="s3://bucket/x.pdf",
        extraction_timestamp=datetime(2026, 5, 2),
        pipeline_version="0.1.0",
    )
    assert fm.pages == []


def test_form_metadata_with_pages():
    fm = FormMetadata(
        form_type="ACORD_125",
        source_document_id="s3://bucket/multi.pdf",
        extraction_timestamp=datetime(2026, 5, 2),
        pipeline_version="0.1.0",
        page_count=3,
        pages=[
            PageMetadata(
                page_number=1,
                page_image_uri="s3://bucket/p1.png",
                page_confidence=0.91,
                tier_used_for_page=1,
            ),
            PageMetadata(
                page_number=2,
                page_image_uri="s3://bucket/p2.png",
                page_confidence=0.0,
                page_status="skipped_blank",
            ),
            PageMetadata(
                page_number=3,
                page_image_uri="s3://bucket/p3.png",
                page_confidence=0.74,
                tier_used_for_page="3a",
                rotation_corrected=2.5,
            ),
        ],
    )
    assert len(fm.pages) == 3
    assert fm.pages[1].page_status == "skipped_blank"
    assert fm.pages[2].rotation_corrected == 2.5


# ---------------------------------------------------------------------------
# Gap 3: DataClass enum (replaces is_pii/is_phi booleans)
# ---------------------------------------------------------------------------


def test_data_class_enum_values():
    assert DataClass.PUBLIC.value == "public"
    assert DataClass.PII.value == "pii"
    assert DataClass.PHI.value == "phi"
    assert DataClass.PCI.value == "pci"


def test_canonical_names_match_attribute_names():
    """Validates the invariant enforced at module import."""
    for cls in (
        IntakeFormBase,
        InsuranceIntakeForm,
        HealthcareIntakeForm,
        HRIntakeForm,
    ):
        for attr_name, meta in get_field_metadata(cls).items():
            assert (
                meta.canonical_name == attr_name
            ), f"{cls.__name__}.{attr_name}: meta.canonical_name={meta.canonical_name!r}"


def test_first_name_data_class_differs_by_vertical():
    """Same canonical name, different DataClass in different verticals."""
    base = get_field_metadata(IntakeFormBase)["first_name"]
    healthcare = get_field_metadata(HealthcareIntakeForm)["first_name"]
    hr = get_field_metadata(HRIntakeForm)["first_name"]
    insurance = get_field_metadata(InsuranceIntakeForm)["first_name"]

    assert base.data_class == DataClass.PII
    assert base.sensitivity == "medium"

    assert healthcare.data_class == DataClass.PHI
    assert healthcare.sensitivity == "high"

    assert hr.data_class == DataClass.PII
    assert hr.sensitivity == "medium"

    assert insurance.data_class == DataClass.PII
    assert insurance.sensitivity == "medium"


def test_all_healthcare_inherited_pii_fields_become_phi():
    """HIPAA's 18 identifiers: every PII field in healthcare context elevates to PHI."""
    healthcare = get_field_metadata(HealthcareIntakeForm)
    base_pii_field_names = [
        name
        for name, m in get_field_metadata(IntakeFormBase).items()
        if m.data_class == DataClass.PII
    ]
    for name in base_pii_field_names:
        assert (
            healthcare[name].data_class == DataClass.PHI
        ), f"{name} is PII on the base but not PHI in HealthcareIntakeForm"
        assert healthcare[name].sensitivity == "high"


def test_bank_credentials_are_pci():
    """Routing+account is a payment credential, classified as PCI."""
    hr = get_field_metadata(HRIntakeForm)
    assert hr["bank_routing_number"].data_class == DataClass.PCI
    assert hr["bank_account_number"].data_class == DataClass.PCI
    assert hr["bank_account_type"].data_class == DataClass.PUBLIC  # checking vs savings


def test_is_baa_required_helper():
    """PHI and PCI always require BAA-eligible routing."""
    hc = get_field_metadata(HealthcareIntakeForm)
    hr = get_field_metadata(HRIntakeForm)
    insurance = get_field_metadata(InsuranceIntakeForm)
    base = get_field_metadata(IntakeFormBase)

    # PHI -> True
    assert is_baa_required(hc["first_name"]) is True
    assert is_baa_required(hc["allergies"]) is True

    # PCI -> True
    assert is_baa_required(hr["bank_routing_number"]) is True
    assert is_baa_required(hr["bank_account_number"]) is True

    # PII -> False (HIPAA mode handled by routing layer, not this helper)
    assert is_baa_required(hr["ssn"]) is False
    assert is_baa_required(insurance["fein"]) is False
    assert is_baa_required(base["first_name"]) is False

    # PUBLIC -> False
    assert is_baa_required(insurance["naics_code"]) is False
    assert is_baa_required(hr["bank_account_type"]) is False


# ---------------------------------------------------------------------------
# JSON round-trip (Step Functions contract)
# ---------------------------------------------------------------------------


def _make_metadata() -> FormMetadata:
    return FormMetadata(
        form_type="CMS_1500_02_12",
        source_document_id="s3://intake-bucket/uploads/abc123.pdf",
        extraction_timestamp=datetime(2026, 5, 2, 14, 30, 0),
        pipeline_version="0.1.0",
        overall_confidence=0.87,
    )


def test_healthcare_form_json_roundtrip():
    form = HealthcareIntakeForm(
        metadata=_make_metadata(),
        first_name=ExtractedField[str](value="Jane", confidence=0.97, tier_used=1),
        last_name=ExtractedField[str](value="Doe", confidence=0.95, tier_used=1),
        date_of_birth=ExtractedField[date](value=date(1985, 3, 15), confidence=0.82, tier_used=1),
        sex=ExtractedField(value="F", confidence=0.99, tier_used=1),
        insurance_member_id=ExtractedField[str](
            value="ABC12345678",
            confidence=0.88,
            tier_used="3a",
            escalation_history=[
                TierAttempt(tier=1, confidence=0.55, reason_escalated="low_confidence"),
            ],
        ),
        allergies=ExtractedField[list[str]](
            value=["penicillin", "sulfa"], confidence=0.72, tier_used="3a"
        ),
        hipaa_acknowledgment=ExtractedField[bool](value=True, confidence=0.99, tier_used=1),
    )

    json_str = form.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["metadata"]["form_type"] == "CMS_1500_02_12"
    assert parsed["first_name"]["value"] == "Jane"
    assert parsed["allergies"]["value"] == ["penicillin", "sulfa"]
    assert parsed["insurance_member_id"]["tier_used"] == "3a"
    assert len(parsed["insurance_member_id"]["escalation_history"]) == 1

    restored = HealthcareIntakeForm.model_validate_json(json_str)
    assert restored.first_name.value == "Jane"
    assert restored.date_of_birth.value == date(1985, 3, 15)
    assert restored.allergies.value == ["penicillin", "sulfa"]


def test_form_json_roundtrip_with_bounding_box():
    """BoundingBox round-trips through JSON cleanly."""
    bb = BoundingBox(page_number=2, x1=100.0, y1=200.0, x2=300.0, y2=240.0)
    form = HRIntakeForm(
        metadata=FormMetadata(
            form_type="W-4_2026",
            source_document_id="s3://x/y.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
        ),
        ssn=ExtractedField[str](
            value="123-45-6789",
            confidence=0.99,
            tier_used=1,
            bounding_box=bb,
        ),
    )
    json_str = form.model_dump_json()
    restored = HRIntakeForm.model_validate_json(json_str)
    assert restored.ssn.bounding_box is not None
    assert restored.ssn.bounding_box.page_number == 2
    assert restored.ssn.bounding_box.x1 == 100.0


def test_form_json_roundtrip_with_pages():
    """PageMetadata list round-trips through Step Functions state."""
    form = InsuranceIntakeForm(
        metadata=FormMetadata(
            form_type="ACORD_125",
            source_document_id="s3://x/y.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
            page_count=2,
            pages=[
                PageMetadata(
                    page_number=1,
                    page_image_uri="s3://x/p1.png",
                    page_confidence=0.92,
                    tier_used_for_page=1,
                ),
                PageMetadata(
                    page_number=2,
                    page_image_uri="s3://x/p2.png",
                    page_confidence=0.0,
                    page_status="skipped_blank",
                ),
            ],
        ),
    )
    restored = InsuranceIntakeForm.model_validate_json(form.model_dump_json())
    assert len(restored.metadata.pages) == 2
    assert restored.metadata.pages[1].page_status == "skipped_blank"


def test_insurance_form_json_roundtrip_with_loss_history():
    form = InsuranceIntakeForm(
        metadata=FormMetadata(
            form_type="ACORD_125",
            source_document_id="s3://bucket/acord-001.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
        ),
        named_insured=ExtractedField[str](value="Acme Widgets, LLC", confidence=0.96, tier_used=1),
        fein=ExtractedField[str](value="12-3456789", confidence=0.91, tier_used=1),
        loss_history=ExtractedField[list[LossEvent]](
            value=[
                LossEvent(
                    occurrence_date=ExtractedField[date](
                        value=date(2024, 6, 15), confidence=0.85, tier_used=2
                    ),
                    description=ExtractedField[str](
                        value="Slip and fall, customer", confidence=0.78, tier_used=2
                    ),
                    amount_paid=ExtractedField[float](value=12500.0, confidence=0.82, tier_used=2),
                    status=ExtractedField(value="closed", confidence=0.95, tier_used=2),
                )
            ],
            confidence=0.80,
            tier_used=2,
        ),
    )
    json_str = form.model_dump_json()
    restored = InsuranceIntakeForm.model_validate_json(json_str)
    assert restored.loss_history.value is not None
    assert len(restored.loss_history.value) == 1
    assert restored.loss_history.value[0].amount_paid.value == 12500.0


def test_hr_form_w4_2026_dependents_credit():
    """W-4 2026 Step 3: $2,200 per qualifying child * 2 = $4,400."""
    form = HRIntakeForm(
        metadata=FormMetadata(
            form_type="W-4_2026",
            source_document_id="s3://bucket/w4-001.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
        ),
        ssn=ExtractedField[str](value="123-45-6789", confidence=0.99, tier_used=1),
        filing_status=ExtractedField(value="married_filing_jointly", confidence=0.98, tier_used=1),
        dependents_credit_amount=ExtractedField[float](value=4400.0, confidence=0.95, tier_used=1),
        exempt_from_withholding=ExtractedField[bool](value=False, confidence=0.99, tier_used=1),
    )
    json_str = form.model_dump_json()
    restored = HRIntakeForm.model_validate_json(json_str)
    assert restored.dependents_credit_amount.value == 4400.0
    assert restored.filing_status.value == "married_filing_jointly"


def test_extra_fields_forbidden():
    """extra='forbid' catches typo'd canonical names at deserialization."""
    bad = {
        "metadata": {
            "form_type": "CMS_1500_02_12",
            "source_document_id": "s3://x/y.pdf",
            "extraction_timestamp": "2026-05-02T00:00:00",
            "pipeline_version": "0.1.0",
            "overall_confidence": 0.5,
        },
        "frist_name": {"value": "Jane", "confidence": 0.99, "tier_used": 1},  # typo
    }
    with pytest.raises(ValidationError):
        HealthcareIntakeForm.model_validate(bad)


# ---------------------------------------------------------------------------
# Vertical isolation: verify subclasses don't leak fields into each other
# ---------------------------------------------------------------------------


def test_insurance_does_not_have_healthcare_fields():
    insurance_fields = set(get_field_metadata(InsuranceIntakeForm).keys())
    assert "patient_id" not in insurance_fields
    assert "ssn" not in insurance_fields
    assert "fein" in insurance_fields


def test_healthcare_does_not_have_insurance_fields():
    healthcare_fields = set(get_field_metadata(HealthcareIntakeForm).keys())
    assert "fein" not in healthcare_fields
    assert "loss_history" not in healthcare_fields
    assert "patient_id" in healthcare_fields


def test_hr_does_not_have_healthcare_fields():
    hr_fields = set(get_field_metadata(HRIntakeForm).keys())
    assert "patient_id" not in hr_fields
    assert "allergies" not in hr_fields
    assert "ssn" in hr_fields


# ---------------------------------------------------------------------------
# Alias seed integrity
# ---------------------------------------------------------------------------


def test_alias_seed_loads():
    with open("alias_table_seed.json") as f:
        data = json.load(f)
    assert "fields" in data
    assert len(data["fields"]) > 0


def test_alias_seed_uses_data_class_not_booleans():
    """After Gap 3 fix, seed JSON should have data_class, not is_pii/is_phi."""
    with open("alias_table_seed.json") as f:
        text = f.read()
    assert "is_pii" not in text
    assert "is_phi" not in text
    assert "data_class" in text


def test_every_canonical_field_in_seed():
    with open("alias_table_seed.json") as f:
        data = json.load(f)
    seed_keys = {(r["canonical_name"], r["vertical"]) for r in data["fields"]}
    for name in get_field_metadata(IntakeFormBase):
        assert (name, "base") in seed_keys, f"base/{name} missing from seed"
    for name in get_field_metadata(InsuranceIntakeForm):
        if name not in get_field_metadata(IntakeFormBase):
            assert (name, "insurance") in seed_keys
    for name in get_field_metadata(HealthcareIntakeForm):
        if name not in get_field_metadata(IntakeFormBase):
            assert (name, "healthcare") in seed_keys
    for name in get_field_metadata(HRIntakeForm):
        if name not in get_field_metadata(IntakeFormBase):
            assert (name, "hr") in seed_keys


def test_seed_fields_have_aliases():
    """Every seed record must have at least one alias."""
    with open("alias_table_seed.json") as f:
        data = json.load(f)
    no_aliases = [(r["canonical_name"], r["vertical"]) for r in data["fields"] if not r["aliases"]]
    assert no_aliases == []


def test_alias_seed_position_zero_is_canonical():
    """
    Position 0 of every record's aliases is the canonical/authoritative
    phrasing — load-bearing for the F1-over-time chart's progressive
    alias-table partition (see docs/eval-methodology.md). Catches accidental
    reordering of aliases that would silently shift the historical chart.
    """
    with open("alias_table_seed.json") as f:
        data = json.load(f)
    by_key = {(r["canonical_name"], r["vertical"]): r["aliases"] for r in data["fields"]}

    # Representative spot-checks across verticals — canonical phrasing per the
    # source standard (CMS-1500, ACORD 125, USCIS I-9, etc.) at position 0.
    expected_canonical = {
        ("first_name", "base"): "First Name",
        ("last_name", "base"): "Last Name",
        ("date_of_birth", "base"): "Date of Birth",
        ("phone", "base"): "Phone",
        ("address_zip", "base"): "ZIP",
        ("named_insured", "insurance"): "Named Insured",
        ("fein", "insurance"): "FEIN",
        ("first_name", "hr"): "First Name (Given Name)",  # USCIS I-9 phrasing
        ("date_of_birth", "hr"): "Date of Birth (mm/dd/yyyy)",  # USCIS I-9
    }
    for key, expected in expected_canonical.items():
        aliases = by_key.get(key)
        assert aliases is not None, f"{key} missing from seed"
        assert aliases[0] == expected, (
            f"{key}: expected canonical '{expected}' at position 0, "
            f"got '{aliases[0]}'. Position 0 ordering is load-bearing "
            f"for the F1-over-time chart partition; do not reorder "
            f"existing aliases without bumping the seed version."
        )


# ---------------------------------------------------------------------------
# Gap 4: compute_form_confidence module-level function
# ---------------------------------------------------------------------------


def _empty_metadata() -> FormMetadata:
    return FormMetadata(
        form_type="W-4_2026",
        source_document_id="s3://x/y.pdf",
        extraction_timestamp=datetime(2026, 5, 2),
        pipeline_version="0.1.0",
    )


def test_compute_confidence_empty_form():
    """No populated fields -> vacuous result, all counts zero."""
    form = HRIntakeForm(metadata=_empty_metadata())
    result = compute_form_confidence(form)
    assert result["min"] == 1.0
    assert result["mean"] == 1.0
    assert result["field_count"] == 0
    assert result["blank_count"] == 0
    assert result["unattempted_count"] > 0  # All the fields left at default


def test_compute_confidence_single_populated_field():
    form = HRIntakeForm(
        metadata=_empty_metadata(),
        ssn=ExtractedField[str](value="123-45-6789", confidence=0.85, tier_used=1),
    )
    result = compute_form_confidence(form)
    assert result["min"] == 0.85
    assert result["mean"] == 0.85
    assert result["field_count"] == 1


def test_compute_confidence_multiple_fields():
    form = HRIntakeForm(
        metadata=_empty_metadata(),
        first_name=ExtractedField[str](value="Jane", confidence=0.97, tier_used=1),
        last_name=ExtractedField[str](value="Doe", confidence=0.93, tier_used=1),
        ssn=ExtractedField[str](value="123-45-6789", confidence=0.65, tier_used="3a"),
    )
    result = compute_form_confidence(form)
    assert result["min"] == 0.65  # ssn is the weakest
    assert abs(result["mean"] - (0.97 + 0.93 + 0.65) / 3) < 1e-9
    assert result["field_count"] == 3


def test_compute_confidence_excludes_confidently_blank():
    """Definition 1: value=None excludes the field, even when extraction was confident."""
    form = HRIntakeForm(
        metadata=_empty_metadata(),
        ssn=ExtractedField[str](value="123-45-6789", confidence=0.65, tier_used=1),
        # emergency_contact_name attempted, model said blank with high confidence
        # (using a field that exists on HRIntakeForm)
    )
    # Mark another field as confidently-blank: use a base field
    form.first_name = ExtractedField[str](value=None, confidence=0.99, tier_used=1)

    result = compute_form_confidence(form)
    # Only ssn counts toward min/mean
    assert result["min"] == 0.65
    assert result["mean"] == 0.65
    assert result["field_count"] == 1
    assert result["blank_count"] == 1  # the confidently-blank first_name


def test_compute_confidence_excludes_unattempted():
    """tier_used=None means never attempted; not in confidence calc, counted separately."""
    form = HRIntakeForm(
        metadata=_empty_metadata(),
        ssn=ExtractedField[str](value="123-45-6789", confidence=0.85, tier_used=1),
    )
    # All other fields are at default ExtractedField() = unattempted
    result = compute_form_confidence(form)
    assert result["field_count"] == 1
    # HRIntakeForm has 13 base + 19 vertical = 32 ExtractedField slots; 1 populated
    # (Some base fields may be overridden in subclass but count is what matters)
    assert result["unattempted_count"] >= 30


def test_compute_confidence_recurses_into_loss_history():
    """Insurance form with nested LossEvent rows: all nested confidences contribute."""
    form = InsuranceIntakeForm(
        metadata=FormMetadata(
            form_type="ACORD_125",
            source_document_id="s3://x/y.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
        ),
        named_insured=ExtractedField[str](value="Acme Widgets, LLC", confidence=0.96, tier_used=1),
        loss_history=ExtractedField[list[LossEvent]](
            value=[
                LossEvent(
                    occurrence_date=ExtractedField[date](
                        value=date(2024, 6, 15), confidence=0.85, tier_used=2
                    ),
                    description=ExtractedField[str](
                        value="Slip and fall",
                        confidence=0.42,
                        tier_used=2,  # weak
                    ),
                    amount_paid=ExtractedField[float](value=12500.0, confidence=0.82, tier_used=2),
                ),
            ],
            confidence=0.80,
            tier_used=2,
        ),
    )
    result = compute_form_confidence(form, recurse=True)
    # Should include named_insured (0.96), loss_history wrapper (0.80),
    # and three nested LossEvent fields (0.85, 0.42, 0.82)
    # Min should be 0.42 from the weak nested field
    assert result["min"] == 0.42


def test_compute_confidence_recurse_false_skips_nested():
    """recurse=False stops at the top level."""
    form = InsuranceIntakeForm(
        metadata=FormMetadata(
            form_type="ACORD_125",
            source_document_id="s3://x/y.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
        ),
        named_insured=ExtractedField[str](value="Acme Widgets, LLC", confidence=0.96, tier_used=1),
        loss_history=ExtractedField[list[LossEvent]](
            value=[
                LossEvent(
                    description=ExtractedField[str](
                        value="bad",
                        confidence=0.10,
                        tier_used=2,  # very weak
                    ),
                ),
            ],
            confidence=0.80,
            tier_used=2,
        ),
    )
    result = compute_form_confidence(form, recurse=False)
    # 0.10 nested confidence should NOT bring down the min
    assert result["min"] == 0.80  # loss_history wrapper, not the nested field
    assert 0.10 not in (result["min"], result["mean"])


def test_compute_confidence_empty_list_value():
    """value=[] is 'not None' - field counts but no recursion happens."""
    form = InsuranceIntakeForm(
        metadata=FormMetadata(
            form_type="ACORD_125",
            source_document_id="s3://x/y.pdf",
            extraction_timestamp=datetime(2026, 5, 2),
            pipeline_version="0.1.0",
        ),
        loss_history=ExtractedField[list[LossEvent]](value=[], confidence=0.95, tier_used=1),
    )
    result = compute_form_confidence(form)
    assert result["field_count"] == 1
    assert result["min"] == 0.95
