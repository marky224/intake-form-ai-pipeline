"""
Canonical intake form schemas for the self-improving intake-form pipeline.

Three verticals share a common base of person/address/contact fields and each
add vertical-specific fields anchored to a recognized standard:

    InsuranceIntakeForm  -> ACORD 125 (Commercial Insurance Application),
                            ACORD 126 (Commercial GL), ACORD 130 (Workers' Comp)
    HealthcareIntakeForm -> CMS-1500 (HCFA), HIPAA 837P transaction set,
                            patient registration / consent / medication intake
    HRIntakeForm         -> USCIS Form I-9 (01/20/2025 edition),
                            IRS Form W-4 (2026 edition), direct deposit auth,
                            emergency contact, benefits enrollment

Every extracted field is wrapped in `ExtractedField[T]`, which carries the
extracted value, the confidence score, the tier that produced it, and the
escalation history. Class-level metadata about each canonical field
(description, PHI/PII status, sensitivity tier) lives in `FieldMeta`
attached via `typing.Annotated` so the routing layer and alias table can
introspect it without instantiating the model.

Design principles:
  * Pydantic v2, JSON-serializable for Step Functions state passing
  * Use Optional[T] (not Union[T, None]) for absent fields
  * Don't pollute the schema with rare fields - documented in RATIONALE.md
  * PHI/PII flags are field-level, declared once on the model class
  * The same canonical field name (e.g. "first_name") can have different
    PHI status in different verticals - HR is PII-only, healthcare is PHI

Out of scope (separate concerns):
  * Database schema for storing extractions
  * Alias normalization algorithm (consumes alias_table_seed.json)
  * Few-shot retrieval format
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar, get_type_hints

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

#: Tier identifier. Strings used for 3a/3b because they're sub-tiers, not
#: ordered ranks - "3b" doesn't mean "after 3a", it means "the escalation
#: variant of tier 3". Encoded as Literal so JSON schema validates it.
TierId = Literal[1, 2, "3a", "3b"]

#: Sensitivity classification used by the routing layer to decide whether
#: a field's value can be sent to non-BAA inference providers.
#: - "low"    : routable to any provider
#: - "medium" : PII; route only to providers with DPA + encryption in transit
#: - "high"   : PHI under HIPAA, or financial account numbers; BAA-only
Sensitivity = Literal["low", "medium", "high"]


class DataClass(str, Enum):
    """
    Regulatory classification of a field's contents.

    Used by the routing layer alongside `Sensitivity` to determine which
    inference providers are eligible for a given extraction. Orthogonal to
    sensitivity: data_class says WHAT kind of regulated data, sensitivity
    says HOW careful within that regime.

    Routing rules:
      PUBLIC -> any provider
      PII    -> any provider with DPA + encryption in transit; HIPAA mode
                may further restrict to BAA-eligible providers
      PHI    -> BAA-eligible providers only (always)
      PCI    -> BAA-eligible providers only (always); payment credentials

    Future extensions (when the pipeline expands beyond the current verticals):
      GLBA (financial institution data), FERPA (educational records),
      GDPR_PERSONAL (EU personal data).
    """

    PUBLIC = "public"
    PII = "pii"
    PHI = "phi"
    PCI = "pci"


def is_baa_required(meta: FieldMeta) -> bool:
    """
    True if this field requires routing to a BAA-eligible provider only.
    PHI and PCI always require BAA. PII routing depends on HIPAA mode
    (handled by the routing layer, not this helper).
    """
    return meta.data_class in (DataClass.PHI, DataClass.PCI)


@dataclass(frozen=True, slots=True)
class FieldMeta:
    """
    Static metadata about a canonical field.

    Attached via `typing.Annotated[...]` so it travels with the field
    annotation and can be introspected by the alias loader, routing layer,
    and audit log without needing a model instance.

    The `canonical_name` MUST match the attribute name on the model class.
    Validated at module import via `_validate_canonical_names()`.

    `data_class` and `sensitivity` are orthogonal:
    - data_class: WHAT kind of regulated data (PUBLIC, PII, PHI, PCI)
    - sensitivity: HOW careful to be within that regime (low, medium, high)
    """

    canonical_name: str
    description: str
    data_class: DataClass = DataClass.PUBLIC
    sensitivity: Sensitivity = "low"
    source_standard: str | None = None  # e.g. "ACORD 125", "CMS-1500 Box 2"


T = TypeVar("T")


class BoundingBox(BaseModel):
    """
    Region on a page identified during extraction.

    Frozen because bounding boxes shouldn't mutate after extraction. The
    page_number is 1-indexed to match human-facing page numbering. Coordinates
    are in page units (typically pixels in the rendered page image, or points
    if extracted from a PDF natively).

    Coordinate convention matches PaddleOCR / most vision OCR systems:
    (x1, y1) is top-left, (x2, y2) is bottom-right. Textract's
    Left/Top/Width/Height format must be converted before populating.
    """

    model_config = ConfigDict(frozen=True)

    page_number: int = Field(ge=1, description="1-indexed page number")
    x1: float = Field(description="Left edge (page units)")
    y1: float = Field(description="Top edge (page units)")
    x2: float = Field(description="Right edge (page units)")
    y2: float = Field(description="Bottom edge (page units)")


class SignatureCapture(BaseModel):
    """
    Signature presence and visual classification.

    Carried as the value type of `signature: ExtractedField[SignatureCapture]`
    on `IntakeFormBase`. Replaces the previous `signature_present: bool`
    representation, which conflated typed and handwritten submission modes
    and prevented the routing layer from making mode-aware escalation
    decisions.

    Field semantics:
    - `present`: binary "is there a signature here" answer
    - `appears_handwritten`: visual classification. True when the cascade is
      confident the signature is handwritten ink (or a tablet-drawn
      approximation thereof). False when confident it is not. None when the
      cascade cannot determine (typically because `present` is False, or
      because the visual signal is too weak to classify).
    - `appears_typed`: visual classification for typed signatures (including
      "/s/ John Doe", typed name in a script font, e-signature blocks).
      Same True/False/None semantics as `appears_handwritten`.

    Both `appears_handwritten` and `appears_typed` can be True simultaneously
    for genuinely ambiguous cases — typed name in a heavy script font that
    visually resembles handwriting, or a tablet-drawn signature that landed
    cleanly enough to look typed. The routing layer treats both-True as a
    review-queue trigger rather than guessing.

    Frozen because extraction outputs shouldn't mutate after the cascade
    produces them.
    """

    model_config = ConfigDict(frozen=True)

    present: bool
    appears_handwritten: bool | None = None
    appears_typed: bool | None = None


class TierAttempt(BaseModel):
    """One attempt to extract a field at a given tier."""

    model_config = ConfigDict(frozen=True)

    tier: TierId
    confidence: float = Field(ge=0.0, le=1.0)
    raw_value: str | None = None
    reason_escalated: (
        Literal["low_confidence", "schema_violation", "validator_failed", "manual_review"] | None
    ) = None
    timestamp: datetime | None = None


class ExtractedField(BaseModel, Generic[T]):
    """
    Wrapper around every extracted value carrying provenance.

    `value` is None when the field was not present on the form OR has not
    yet been extracted. Use `tier_used is None` to distinguish "not yet
    attempted" from "attempted, found nothing" (the latter has tier_used
    set and confidence reflecting the absence judgment).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: T | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tier_used: TierId | None = None
    escalation_history: list[TierAttempt] = Field(default_factory=list)
    raw_text: str | None = None
    bounding_box: BoundingBox | None = None


def _ef() -> ExtractedField:
    """Default factory for ExtractedField fields. Pydantic doesn't accept
    `ExtractedField` directly as a default_factory because of the generic."""
    return ExtractedField()


# ---------------------------------------------------------------------------
# Form-level metadata
# ---------------------------------------------------------------------------


class PageMetadata(BaseModel):
    """
    Per-page operational metadata for a multi-page form extraction.

    The pipeline renders each page of the source document to an image, runs
    the chosen tier's extraction model against it, and records:
    - which tier produced the extraction for this page
    - the aggregate confidence for fields extracted from this page
    - the rotation correction applied (if any)
    - the page status (extracted normally, blank, failed, manual review only)

    page_status drives routing decisions: blank pages and failed pages should
    not be re-escalated to higher tiers looking for fields that aren't there.
    """

    page_number: int = Field(ge=1, description="1-indexed page number")
    page_image_uri: str = Field(description="S3 URI of the rendered page image (PNG or JPEG)")
    page_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tier_used_for_page: TierId | None = None
    page_status: Literal["extracted", "skipped_blank", "failed", "manual_only"] = "extracted"
    rotation_corrected: float | None = Field(
        default=None,
        description="Degrees of rotation correction applied; None if no rotation needed",
    )


class FormMetadata(BaseModel):
    """Top-level metadata about an extraction run, not extracted from the document."""

    form_type: str  # "ACORD_125", "CMS_1500_02_12", "I-9_01_20_25", "W-4_2026", etc.
    source_document_id: str  # S3 key, URI, or other stable identifier
    extraction_timestamp: datetime
    pipeline_version: str
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    page_count: int | None = None
    pages: list[PageMetadata] = Field(
        default_factory=list,
        description="Per-page extraction metadata; empty for single-page forms",
    )
    routing_decision: str | None = None  # which document classifier path was taken


# ---------------------------------------------------------------------------
# Base intake form: fields common to every vertical
# ---------------------------------------------------------------------------


class IntakeFormBase(BaseModel):
    """
    Fields that appear on intake forms across insurance, healthcare, and HR.

    Subclasses override fields when the PHI/PII classification needs to
    change for that vertical (e.g. `first_name` is PII-only on a W-4 but
    PHI on a CMS-1500 because it's one of HIPAA's 18 identifiers when
    associated with health information).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    metadata: FormMetadata

    # -- Person identification ------------------------------------------------

    first_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="first_name",
            description="Given name (first name) of the primary subject of the form.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    middle_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="middle_name",
            description="Middle name or middle initial. May be a single character.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    last_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="last_name",
            description="Family name (surname) of the primary subject. May contain spaces or hyphens.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    # -- Address --------------------------------------------------------------

    address_street: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_street",
            description="Street number and name of the residential or mailing address.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    address_apt: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_apt",
            description="Apartment, suite, or unit number. Often blank.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    address_city: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_city",
            description="City or town of the address.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    address_state: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_state",
            description="Two-letter US state code (or full state name). Normalize to USPS code downstream.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    address_zip: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_zip",
            description="5- or 9-digit US ZIP code (string, not int, to preserve leading zeros).",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    # -- Contact and identifiers ---------------------------------------------

    date_of_birth: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="date_of_birth",
            description="Date of birth of the primary subject. Forms typically show MM/DD/YYYY.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    phone: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="phone",
            description="Primary contact phone number. Stored as-extracted; normalize to E.164 downstream.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    email: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="email",
            description="Primary contact email address.",
            data_class=DataClass.PII,
            sensitivity="medium",
        ),
    ] = Field(default_factory=_ef)

    # -- Signature and date ---------------------------------------------------

    signature: Annotated[
        ExtractedField[SignatureCapture],
        FieldMeta(
            canonical_name="signature",
            description="Signature capture: presence on the form plus visual classification (handwritten / typed / ambiguous). See SignatureCapture.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
        ),
    ] = Field(default_factory=_ef)

    date_signed: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="date_signed",
            description="Date the form was signed by the primary subject.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
        ),
    ] = Field(default_factory=_ef)


# ---------------------------------------------------------------------------
# Insurance vertical
# ---------------------------------------------------------------------------


class LossEvent(BaseModel):
    """One row from the ACORD 125 loss history grid (5-year history typical)."""

    occurrence_date: ExtractedField[date] = Field(default_factory=_ef)
    description: ExtractedField[str] = Field(default_factory=_ef)
    amount_paid: ExtractedField[float] = Field(default_factory=_ef)
    amount_reserved: ExtractedField[float] = Field(default_factory=_ef)
    status: ExtractedField[Literal["open", "closed"]] = Field(default_factory=_ef)


class InsuranceIntakeForm(IntakeFormBase):
    """
    Commercial insurance application fields anchored to ACORD 125
    (Applicant Information Section), with optional loss history rows
    drawn from the 5-year (10 in RI) loss history table.

    Sources:
      ACORD 125 (2007/10), Commercial Insurance Application
      ACORD 126, Commercial General Liability
      ACORD 130, Workers' Compensation Application
    """

    # Most insurance forms are commercial. The "applicant" is a business entity,
    # so first_name/last_name represent the contact person and named_insured
    # represents the legal entity. For sole-proprietor policies, named_insured
    # matches the personal name.

    named_insured: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="named_insured",
            description="Legal entity name as filed with the state, including suffixes (Inc., LLC, LP). Most-bounced field on ACORD 125.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="ACORD 125 - Applicant",
        ),
    ] = Field(default_factory=_ef)

    dba_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="dba_name",
            description="Doing-Business-As name. Distinct from named_insured; appears in its own field on ACORD 125.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="ACORD 125 - DBA",
        ),
    ] = Field(default_factory=_ef)

    fein: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="fein",
            description="Federal Employer Identification Number (9 digits, often formatted XX-XXXXXXX). Used by carriers as primary submission key.",
            data_class=DataClass.PII,
            sensitivity="high",
            source_standard="ACORD 125 - FEIN",
        ),
    ] = Field(default_factory=_ef)

    entity_type: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="entity_type",
            description="Form of business organization (e.g. Corporation, LLC, Partnership, Sole Proprietor, Joint Venture, Trust).",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    naics_code: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="naics_code",
            description="6-digit NAICS industry classification code. SIC code (4-digit) treated as alias.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    business_description: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="business_description",
            description="Free-text description of primary operations. Free-form, often multi-line.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125 - Description of Primary Operations",
        ),
    ] = Field(default_factory=_ef)

    date_business_started: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="date_business_started",
            description="Date business operations commenced. Sometimes only year is supplied.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    effective_date: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="effective_date",
            description="Policy effective (start) date.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    expiration_date: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="expiration_date",
            description="Policy expiration (end) date. Typically effective_date + 1 year.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    lines_of_business: Annotated[
        ExtractedField[list[str]],
        FieldMeta(
            canonical_name="lines_of_business",
            description="Coverages requested. Multi-select on ACORD 125: GL, Property, Crime, Auto, Umbrella, Workers' Comp, etc.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125 - Lines of Business",
        ),
    ] = Field(default_factory=_ef)

    general_aggregate_limit: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="general_aggregate_limit",
            description="General aggregate liability limit in dollars (CGL section, ACORD 125/126).",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125/126 - General Aggregate",
        ),
    ] = Field(default_factory=_ef)

    each_occurrence_limit: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="each_occurrence_limit",
            description="Per-occurrence liability limit in dollars.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125/126 - Each Occurrence",
        ),
    ] = Field(default_factory=_ef)

    deductible: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="deductible",
            description="Deductible or self-insured retention amount in dollars.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    premium: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="premium",
            description="Total annual premium in dollars. Often blank at quote stage.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    producer_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="producer_name",
            description="Producing agent or broker name. Often the agency name plus an individual.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="ACORD 125 - Producer",
        ),
    ] = Field(default_factory=_ef)

    producer_license_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="producer_license_number",
            description="State producer license number (required in FL) or National Producer Number (NPN).",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125",
        ),
    ] = Field(default_factory=_ef)

    prior_carrier: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="prior_carrier",
            description="Name of the prior insurance carrier. Underwriting signal.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125 - Prior Carrier Information",
        ),
    ] = Field(default_factory=_ef)

    loss_history: Annotated[
        ExtractedField[list[LossEvent]],
        FieldMeta(
            canonical_name="loss_history",
            description="Five-year (ten in RI) loss history grid. Each row is a LossEvent.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="ACORD 125 - Loss History",
        ),
    ] = Field(default_factory=_ef)


# ---------------------------------------------------------------------------
# Healthcare vertical
# ---------------------------------------------------------------------------


class HealthcareIntakeForm(IntakeFormBase):
    """
    Patient intake fields anchored to CMS-1500 (02/12), HIPAA 837P transaction
    set field names where applicable, and common patient registration / consent /
    medication intake forms.

    HIPAA reminder: under 45 CFR 164.514(b)(2), patient name, address, dates
    (other than year), phone, email, SSN, MRN, etc. are all part of the 18
    identifiers. Inherited base fields are overridden here to elevate
    sensitivity from "medium" (PII) to "high" (PHI).
    """

    # -- Override base PII fields to PHI in healthcare context ----------------

    first_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="first_name",
            description="Patient first name. PHI under HIPAA when associated with health information.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 2 (Patient's Name)",
        ),
    ] = Field(default_factory=_ef)

    middle_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="middle_name",
            description="Patient middle name or initial. PHI in healthcare context.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 2",
        ),
    ] = Field(default_factory=_ef)

    last_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="last_name",
            description="Patient last name. PHI in healthcare context.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 2",
        ),
    ] = Field(default_factory=_ef)

    address_street: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_street",
            description="Patient street address. PHI under HIPAA.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 5",
        ),
    ] = Field(default_factory=_ef)

    address_apt: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_apt",
            description="Patient apartment / suite / unit. PHI under HIPAA (geographic subdivision).",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 5",
        ),
    ] = Field(default_factory=_ef)

    address_city: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_city",
            description="Patient city. PHI under HIPAA.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 5",
        ),
    ] = Field(default_factory=_ef)

    address_state: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_state",
            description="Patient state. PHI in combination with other identifiers.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 5",
        ),
    ] = Field(default_factory=_ef)

    address_zip: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="address_zip",
            description="Patient ZIP code. PHI in combination with other identifiers (full ZIP, not just first 3 digits).",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 5",
        ),
    ] = Field(default_factory=_ef)

    date_of_birth: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="date_of_birth",
            description="Patient date of birth. PHI under HIPAA (full date with day).",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 3",
        ),
    ] = Field(default_factory=_ef)

    phone: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="phone",
            description="Patient phone. PHI under HIPAA.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 5",
        ),
    ] = Field(default_factory=_ef)

    email: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="email",
            description="Patient email. PHI under HIPAA when associated with health info.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    # -- Healthcare-specific fields ------------------------------------------

    patient_id: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="patient_id",
            description="Medical Record Number (MRN), chart number, or patient account number.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="HIPAA 18 identifiers - medical record number",
        ),
    ] = Field(default_factory=_ef)

    sex: Annotated[
        ExtractedField[Literal["M", "F", "U"]],
        FieldMeta(
            canonical_name="sex",
            description="Sex assigned at birth, as captured on the form. Some forms use Gender; treated as alias.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 3",
        ),
    ] = Field(default_factory=_ef)

    insurance_member_id: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="insurance_member_id",
            description="Subscriber/member ID from the insurance card. CMS-1500 Box 1a; HIPAA 837P NM109 (NM101=IL).",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 1a; 837P loop 2010BA NM109",
        ),
    ] = Field(default_factory=_ef)

    insurance_group_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="insurance_group_number",
            description="Group number from the insurance card.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 11",
        ),
    ] = Field(default_factory=_ef)

    insurance_plan_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="insurance_plan_name",
            description="Insurance plan or program name (e.g. 'Blue Cross Blue Shield of Texas PPO').",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 11c",
        ),
    ] = Field(default_factory=_ef)

    subscriber_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="subscriber_name",
            description="Insured/subscriber/policyholder name when different from patient. Last, First MI format.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 4; 837P loop 2010BA",
        ),
    ] = Field(default_factory=_ef)

    subscriber_relationship: Annotated[
        ExtractedField[Literal["self", "spouse", "child", "other"]],
        FieldMeta(
            canonical_name="subscriber_relationship",
            description="Patient's relationship to the insured/subscriber.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 6",
        ),
    ] = Field(default_factory=_ef)

    subscriber_dob: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="subscriber_dob",
            description="Subscriber date of birth, when subscriber differs from patient.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 11a",
        ),
    ] = Field(default_factory=_ef)

    primary_care_physician: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="primary_care_physician",
            description="Primary care physician (PCP) name, or referring physician on a CMS-1500.",
            data_class=DataClass.PHI,
            sensitivity="high",
            source_standard="CMS-1500 Box 17 (Referring Provider)",
        ),
    ] = Field(default_factory=_ef)

    reason_for_visit: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="reason_for_visit",
            description="Chief complaint or reason for today's visit. Free text.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    allergies: Annotated[
        ExtractedField[list[str]],
        FieldMeta(
            canonical_name="allergies",
            description="Known allergies (drug, food, environmental). 'NKDA' (No Known Drug Allergies) is a common explicit value.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    current_medications: Annotated[
        ExtractedField[list[str]],
        FieldMeta(
            canonical_name="current_medications",
            description="List of current/prior medications, often including dose and frequency in free text.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    medical_history_conditions: Annotated[
        ExtractedField[list[str]],
        FieldMeta(
            canonical_name="medical_history_conditions",
            description="Past medical history conditions. Often a checkbox grid plus free text.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    emergency_contact_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="emergency_contact_name",
            description="Name of the patient's emergency contact.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    emergency_contact_phone: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="emergency_contact_phone",
            description="Phone number of the patient's emergency contact.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    emergency_contact_relationship: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="emergency_contact_relationship",
            description="Relationship of emergency contact to patient (spouse, parent, child, friend, etc.).",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    pharmacy_preference: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="pharmacy_preference",
            description="Preferred pharmacy name and location for e-prescribing.",
            data_class=DataClass.PHI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    hipaa_acknowledgment: Annotated[
        ExtractedField[bool],
        FieldMeta(
            canonical_name="hipaa_acknowledgment",
            description="Whether the patient acknowledged receipt of the Notice of Privacy Practices. Boolean: signed/checked or not.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="HIPAA Privacy Rule 45 CFR 164.520(c)",
        ),
    ] = Field(default_factory=_ef)


# ---------------------------------------------------------------------------
# HR / Employment vertical
# ---------------------------------------------------------------------------


class HRIntakeForm(IntakeFormBase):
    """
    Employment intake fields drawn from USCIS Form I-9 (01/20/2025 edition),
    IRS Form W-4 (2026 edition), direct deposit authorization, and benefits
    enrollment. Bank account fields are flagged sensitivity="high" because
    routing+account number is functionally a payment credential.
    """

    # -- I-9 Section 1 specific ----------------------------------------------

    ssn: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="ssn",
            description="U.S. Social Security Number. Required on W-4; required on I-9 only if employer participates in E-Verify.",
            data_class=DataClass.PII,
            sensitivity="high",
            source_standard="W-4 Step 1; I-9 Section 1",
        ),
    ] = Field(default_factory=_ef)

    other_last_names_used: Annotated[
        ExtractedField[list[str]],
        FieldMeta(
            canonical_name="other_last_names_used",
            description="Maiden name or any other legal last names previously used. I-9 Section 1.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="I-9 Section 1",
        ),
    ] = Field(default_factory=_ef)

    citizenship_status: Annotated[
        ExtractedField[
            Literal[
                "us_citizen",
                "noncitizen_national",
                "lawful_permanent_resident",
                "alien_authorized_to_work",
            ]
        ],
        FieldMeta(
            canonical_name="citizenship_status",
            description="I-9 Section 1 attestation. Four options as of 01/20/2025 edition (reverted 'noncitizen authorized to work' to 'alien authorized to work').",
            data_class=DataClass.PII,
            sensitivity="high",
            source_standard="I-9 Section 1, Boxes 1-4",
        ),
    ] = Field(default_factory=_ef)

    uscis_a_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="uscis_a_number",
            description="USCIS Number / Alien Registration Number (A-Number). Required for permanent residents and aliens authorized to work on I-9.",
            data_class=DataClass.PII,
            sensitivity="high",
            source_standard="I-9 Section 1",
        ),
    ] = Field(default_factory=_ef)

    i94_admission_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="i94_admission_number",
            description="Form I-94 admission number for noncitizens authorized to work. I-9 Section 1 alternative to A-Number.",
            data_class=DataClass.PII,
            sensitivity="high",
            source_standard="I-9 Section 1",
        ),
    ] = Field(default_factory=_ef)

    foreign_passport_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="foreign_passport_number",
            description="Foreign passport number for noncitizens, including country of issuance. I-9 Section 1 alternative.",
            data_class=DataClass.PII,
            sensitivity="high",
            source_standard="I-9 Section 1",
        ),
    ] = Field(default_factory=_ef)

    work_authorization_expiration: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="work_authorization_expiration",
            description="Date employment authorization expires (for aliens authorized to work). Drives reverification timing.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="I-9 Section 1",
        ),
    ] = Field(default_factory=_ef)

    employee_start_date: Annotated[
        ExtractedField[date],
        FieldMeta(
            canonical_name="employee_start_date",
            description="First day of employment. Section 2 of I-9 must be completed within 3 business days of this date.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="I-9 Section 2",
        ),
    ] = Field(default_factory=_ef)

    employer_name: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="employer_name",
            description="Employer's business or organization name.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="I-9 Section 2; W-4 employer section",
        ),
    ] = Field(default_factory=_ef)

    # -- W-4 specific --------------------------------------------------------

    filing_status: Annotated[
        ExtractedField[
            Literal[
                "single_or_mfs",
                "married_filing_jointly",
                "head_of_household",
            ]
        ],
        FieldMeta(
            canonical_name="filing_status",
            description="Tax filing status, W-4 Step 1(c). MFS is grouped with Single on the W-4.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="W-4 (2026) Step 1(c)",
        ),
    ] = Field(default_factory=_ef)

    multiple_jobs_indicator: Annotated[
        ExtractedField[bool],
        FieldMeta(
            canonical_name="multiple_jobs_indicator",
            description="W-4 Step 2(c) checkbox: employee or spouse holds multiple jobs of similar pay.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="W-4 (2026) Step 2(c)",
        ),
    ] = Field(default_factory=_ef)

    dependents_credit_amount: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="dependents_credit_amount",
            description="W-4 Step 3 total. $2,200 per qualifying child (TY2026 OBBBA value, up from $2,000) plus $500 per other dependent.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="W-4 (2026) Step 3",
        ),
    ] = Field(default_factory=_ef)

    other_income: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="other_income",
            description="W-4 Step 4(a). Other estimated annual income not from jobs.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="W-4 (2026) Step 4(a)",
        ),
    ] = Field(default_factory=_ef)

    deductions: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="deductions",
            description="W-4 Step 4(b). Estimated deductions other than the standard deduction.",
            data_class=DataClass.PII,
            sensitivity="medium",
            source_standard="W-4 (2026) Step 4(b)",
        ),
    ] = Field(default_factory=_ef)

    extra_withholding: Annotated[
        ExtractedField[float],
        FieldMeta(
            canonical_name="extra_withholding",
            description="W-4 Step 4(c). Additional dollar amount to withhold per pay period.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="W-4 (2026) Step 4(c)",
        ),
    ] = Field(default_factory=_ef)

    exempt_from_withholding: Annotated[
        ExtractedField[bool],
        FieldMeta(
            canonical_name="exempt_from_withholding",
            description="W-4 (2026) exemption checkbox. New explicit checkbox in 2026 form (previously a written notation).",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
            source_standard="W-4 (2026) Exempt section",
        ),
    ] = Field(default_factory=_ef)

    # -- Direct deposit ------------------------------------------------------

    bank_routing_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="bank_routing_number",
            description="9-digit ABA routing number. Treated as high-sensitivity because routing+account = payment credential.",
            data_class=DataClass.PCI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    bank_account_number: Annotated[
        ExtractedField[str],
        FieldMeta(
            canonical_name="bank_account_number",
            description="Bank account number for direct deposit. Variable length. High sensitivity.",
            data_class=DataClass.PCI,
            sensitivity="high",
        ),
    ] = Field(default_factory=_ef)

    bank_account_type: Annotated[
        ExtractedField[Literal["checking", "savings"]],
        FieldMeta(
            canonical_name="bank_account_type",
            description="Type of account for direct deposit.",
            data_class=DataClass.PUBLIC,
            sensitivity="low",
        ),
    ] = Field(default_factory=_ef)


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------


def get_field_metadata(model_class: type[BaseModel]) -> dict[str, FieldMeta]:
    """
    Return canonical_name -> FieldMeta for all annotated fields on a model.

    Walks the MRO so subclass overrides take precedence over base class.
    Used by the alias loader and the routing layer at runtime.
    """
    hints = get_type_hints(model_class, include_extras=True)
    result: dict[str, FieldMeta] = {}
    for field_name, hint in hints.items():
        # Annotated[X, FieldMeta(...)] -> __metadata__ is a tuple
        meta = getattr(hint, "__metadata__", ())
        for m in meta:
            if isinstance(m, FieldMeta):
                result[field_name] = m
                break
    return result


def _validate_canonical_names() -> None:
    """Sanity check at import time: FieldMeta.canonical_name must match attribute name."""
    for cls in (
        IntakeFormBase,
        InsuranceIntakeForm,
        HealthcareIntakeForm,
        HRIntakeForm,
    ):
        for attr_name, meta in get_field_metadata(cls).items():
            if meta.canonical_name != attr_name:
                raise ValueError(
                    f"{cls.__name__}.{attr_name}: FieldMeta.canonical_name="
                    f"{meta.canonical_name!r} does not match attribute name."
                )


_validate_canonical_names()


def field_metadata_as_dict(meta: FieldMeta) -> dict[str, Any]:
    """Convert a FieldMeta to a plain dict (for JSON export)."""
    return asdict(meta)


# ---------------------------------------------------------------------------
# Aggregate confidence computation (used by routing layer, eval harness, UI)
# ---------------------------------------------------------------------------


def compute_form_confidence(
    form: BaseModel,
    recurse: bool = True,
) -> dict[str, float | int]:
    """
    Compute aggregate confidence statistics across populated ExtractedField
    instances in a form.

    A field is "populated" if value is not None. Confidently-blank fields
    (value=None, tier_used set) and unattempted fields (tier_used=None) are
    excluded from the confidence calculation but counted separately for
    reviewer UI display.

    If recurse=True, walks into nested BaseModel instances inside list-valued
    extracted fields (e.g., LossEvent rows in InsuranceIntakeForm.loss_history).
    Recursion is unbounded - Pydantic models cannot have cycles.

    The min value is used by the routing layer for escalation decisions
    (worst field determines whether to send the form to a more capable tier).
    The mean value is used by the eval harness for tracking quality over time.
    The blank/unattempted counts are surfaced in the reviewer UI to highlight
    fields requiring human attention.

    Returns:
        {
            "min": float,             # Min confidence across populated fields
            "mean": float,            # Mean confidence across populated fields
            "field_count": int,       # Number of populated fields
            "blank_count": int,       # Confidently-blank (value=None, tier_used set)
            "unattempted_count": int, # Never attempted (tier_used=None)
        }

    For an empty form (no populated fields), returns
    {"min": 1.0, "mean": 1.0, "field_count": 0, ...}.
    """
    confidences: list[float] = []
    blank_count = 0
    unattempted_count = 0

    def _walk(obj: BaseModel) -> None:
        nonlocal blank_count, unattempted_count

        for field_name in type(obj).model_fields:
            attr = getattr(obj, field_name)

            if isinstance(attr, ExtractedField):
                if attr.value is not None:
                    confidences.append(attr.confidence)
                    # Recurse into list-of-BaseModel values (e.g., loss_history)
                    if recurse and isinstance(attr.value, list):
                        for item in attr.value:
                            if isinstance(item, BaseModel):
                                _walk(item)
                elif attr.tier_used is not None:
                    # Confidently blank: extraction was attempted, returned no value
                    blank_count += 1
                else:
                    # Never attempted
                    unattempted_count += 1

    _walk(form)

    if not confidences:
        return {
            "min": 1.0,
            "mean": 1.0,
            "field_count": 0,
            "blank_count": blank_count,
            "unattempted_count": unattempted_count,
        }

    return {
        "min": min(confidences),
        "mean": sum(confidences) / len(confidences),
        "field_count": len(confidences),
        "blank_count": blank_count,
        "unattempted_count": unattempted_count,
    }


__all__ = [
    "TierId",
    "Sensitivity",
    "DataClass",
    "FieldMeta",
    "BoundingBox",
    "SignatureCapture",
    "PageMetadata",
    "TierAttempt",
    "ExtractedField",
    "FormMetadata",
    "LossEvent",
    "IntakeFormBase",
    "InsuranceIntakeForm",
    "HealthcareIntakeForm",
    "HRIntakeForm",
    "get_field_metadata",
    "field_metadata_as_dict",
    "is_baa_required",
    "compute_form_confidence",
]
