# Intake Form Schema Design Rationale

**Status:** Schema as of May 5, 2026 — gap fixes applied and validated; SignatureCapture refactor applied May 5, 2026
**Files:** `intake_schemas.py`, `alias_table_seed.json`, `build_alias_seed.py`, `test_intake_schemas.py`
**Author:** Mark Marquez
**Date:** May 5, 2026 (revised from May 2, 2026 original)

This document reflects the schema after five architectural changes were applied during build review:

1. `DataClass` enum replaces `is_pii`/`is_phi` boolean flags
2. `BoundingBox` typed model replaces the bounding-box tuple
3. `PageMetadata` model added for multi-page form tracking
4. `compute_form_confidence` module-level function for aggregate confidence reporting
5. `SignatureCapture` sub-model replaces the `signature_present: bool` representation

All 40 tests pass; ruff/black clean against the codified `pyproject.toml` lint config (ruff 0.7.4 + black 24.10.0, pinned to match `.pre-commit-config.yaml`). The schema currently covers three verticals (Insurance, Healthcare, HR); the build plan calls for dropping Insurance and HR in Phase 4 and adding `BusinessDocumentForm` against DocILE's 55-field taxonomy. The vertical-specific classes are retained in code as future-extensibility examples per the locked architectural decision.

---

## 1. What's in the base vs. what's vertical-specific

The base class holds only the fields that appear under recognizable variant phrasings on **every one** of the three verticals. Anything that's only-on-one-or-two verticals belongs in the subclass, even if it's tempting to centralize. Inheritance is for shared behavior, not shared shape.

### Base fields (13 canonical fields, all PII, none PHI)

Person identity, contact, address, signature, date signed.

These are present on ACORD 125 (applicant contact name and address), CMS-1500 (boxes 2, 5, 12), I-9 (Section 1 attestation block), and W-4 (Step 1). The exact phrasings differ across forms — "First Name (Given Name)" on the I-9 versus "Patient's Name" on the CMS-1500 versus "Applicant" on the ACORD 125 — but the canonical concept is identical and the alias table absorbs the variation.

### Vertical-specific fields

| Vertical | Field count | Anchor standard |
|---|---|---|
| Insurance | 18 vertical-specific + 13 base = 31 total | ACORD 125 (2007/10), Commercial Insurance Application |
| Healthcare | 18 vertical-specific + 13 base = 31 total | CMS-1500 (02/12) + HIPAA 837P transaction set |
| HR | 19 vertical-specific + 13 base = 32 total | USCIS Form I-9 (01/20/2025) + IRS Form W-4 (2026) |

The schemas are deliberately lean — if a field is rare, document it but don't add it to the canonical schema. Things excluded:

- ACORD: Crime/Property limit grids, foreign operations questions, fidelity loss history (loss history is in for GL only), state-specific fraud warnings
- Healthcare: Service-line detail from CMS-1500 boxes 24A–24J (these belong to claims processing, not intake), referring NPI, prior-authorization number
- HR: I-9 Section 2 document number lists (employer fills these in, not employee), W-4 multi-job worksheet line items, benefits enrollment elections beyond direct deposit

If a downstream user needs one of these, they extend the subclass — they shouldn't have to fight the canonical schema.

### Why `signature` and `date_signed` stay on the base

Initially considered: ACORD has multiple signatures (producer + applicant), CMS-1500 has two (patient in Box 12, physician in Box 31), I-9 has three (employee + preparer + employer). So "the signature" isn't really a single thing.

Decided to keep it on the base anyway. The base `signature` and `date_signed` represent the **primary subject's** signature on the form. The `signature` field carries a `SignatureCapture` sub-model that records both presence and visual classification (handwritten / typed / ambiguous) — see Section 12 for the full rationale on why this richer representation replaces the original boolean. Multi-party signature handling is a downstream concern that needs its own model (probably a `SignatureBlock` list on the metadata). Keeping the base lean beats trying to model every signature variant up front.

### Signature rendering parameters for synthetic data generation

The locked synthetic data pipeline (Phase 3) renders signatures in two modes — typed and handwritten — to give the cascade real diversity. Specifics that the Phase 3 implementer should treat as locked input rather than re-deciding:

**Typed signatures.** Use a single regular sans-serif font: `Arial`. Fixed font choice keeps typed signatures visually consistent (typed PDFs typically render in a standard system font); the variation is supposed to live in the handwritten mode, not typed.

**Handwritten signatures.** Use one of three Google Fonts handwriting families, randomly selected per signature: `Caveat`, `Sacramento`, `Homemade Apple`. Three fonts gives enough handwriting-style variation that the cascade can't memorize a single shape; more fonts would introduce noise without adding informative diversity.

**SVG ink-bleed filter (handwritten only).** Applied via SVG `<filter>` with `<feGaussianBlur stdDeviation="0.5"/>` plus a subtle `<feColorMatrix>` darkening (slight increase in alpha values, no hue shift). The blur simulates ink absorption into paper; the darkening simulates pen pressure variance. Filter applies only to handwritten signatures — typed signatures remain pixel-sharp.

**Rotation jitter.** Handwritten signatures rotate by `±3 degrees` (uniform random per signature). Typed signatures rotate `0 degrees` — typed PDFs are perfectly axis-aligned in real-world submissions.

**Distribution.** 70% typed / 30% handwritten via a single seeded `random.random() < 0.7` check per signature instance. Reflects approximate real-world submission rates: most modern intake forms are submitted online with typed signatures; handwritten remains common for in-person and tablet-based workflows.

**Reproducibility seed.** Single project-wide seed defined in `synthetic_data/render/config.py`. The synthetic dataset for any given commit + seed combination is reproducible byte-for-byte. Changing the seed regenerates the corpus; the same seed always produces the same documents.

**Phase 3 implementation effort.** ~1-2 hours total: ~50-80 lines of Python in `synthetic_data/render/signature.py`, ~15 lines of CSS/SVG filter definitions, integration with the existing Playwright template. Testing: visual sanity check that typed and handwritten outputs look genuinely distinct; programmatic check that the 70/30 distribution holds across a batch of ~500 generated documents.

These parameters are locked unless Phase 6 F1 measurement shows the cascade can't generalize from font-rendered handwriting to real handwriting. If that materializes, the upgrade path — public datasets like IAM, or a small handwriting GAN — is documented in `docs/production-roadmap.md`.

---

## 2. The `ExtractedField[T]` wrapper

Every field on every form is wrapped in `ExtractedField[T]`. This is non-negotiable for the pipeline architecture: routing and review depend on knowing which tier produced each value, with what confidence, and how it got there.

```python
class ExtractedField(BaseModel, Generic[T]):
    value: Optional[T] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tier_used: Optional[TierId] = None
    escalation_history: list[TierAttempt] = Field(default_factory=list)
    raw_text: Optional[str] = None
    bounding_box: Optional[BoundingBox] = None
```

A few specific decisions worth flagging:

**`tier_used: Optional[TierId]` not just `TierId`** — `None` distinguishes "this field has not been attempted yet" (e.g., not present on the form, or not yet processed) from "extracted at tier 1." This matters for the eval harness, where attempted vs. not-attempted must be counted separately when computing F1.

**`raw_text` separate from `value`** — `value` is the typed, normalized output (e.g., `date(1985, 3, 15)`). `raw_text` is the OCR text before normalization (e.g., `"3/15/85"`). Keeping both lets the correction loop train against normalization errors specifically, not just OCR errors.

**`bounding_box` is now a typed `BoundingBox` model, not a tuple** — see Section 9 for the rationale.

**`TierAttempt` is frozen** — escalation history is append-only; mutating a past attempt would erase audit trail.

---

## 3. Data classification via `DataClass` enum

The original schema used two boolean flags — `is_pii` and `is_phi` — alongside a `sensitivity` field. During architectural review, the inconsistency surfaced: several fields were flagged `is_phi=True` but `is_pii=False`, which is technically incorrect under HIPAA. PHI is statutorily a subset of PII, not a parallel category. Same-named fields had different combinations of flags depending on vertical, and the routing layer would have needed to compute `is_pii OR is_phi` to make decisions — extra logic that obscures intent.

The fix: replace both booleans with a single enum.

```python
class DataClass(str, Enum):
    PUBLIC = "public"   # No restrictions, routable anywhere
    PII = "pii"         # Personal info, not health-related
    PHI = "phi"         # Health info; HIPAA-mode → BAA-eligible providers only
    PCI = "pci"         # Payment credentials (bank routing/account)
```

### Why an enum over booleans

- **Forces correctness at declaration time.** A field can't simultaneously be marked PHI without PII; the type system makes it impossible.
- **Forward-extensible.** Adding `GLBA`, `FERPA`, or `GDPR_PERSONAL` later doesn't require new boolean flags or routing logic changes — just a new enum value and one routing rule.
- **Cleaner downstream code.** The `is_baa_required(meta)` helper becomes `meta.data_class in (DataClass.PHI, DataClass.PCI)`, replacing what was previously an ambiguous OR of two flags.

### Why `Sensitivity` is retained as an orthogonal axis

`data_class` and `sensitivity` answer different questions:
- `data_class`: WHAT kind of regulated data this is (the regulatory regime that applies)
- `sensitivity`: HOW careful the routing layer should be within that regime (low / medium / high)

These are independent. A `DataClass.PII` field with `sensitivity="low"` (e.g., a person's first name on a public form) is routed differently than a `DataClass.PII` field with `sensitivity="high"` (e.g., SSN). Collapsing them would lose information.

The HIPAA-mode routing rule is what `data_class` drives:
- `DataClass.PUBLIC`: any provider
- `DataClass.PII`: HIPAA-mode-dependent (handled by routing layer; BAA-only when HIPAA mode is on, otherwise unrestricted)
- `DataClass.PHI`: BAA-eligible providers only (always)
- `DataClass.PCI`: BAA-eligible providers only (always)

### Why bank credentials moved to `DataClass.PCI`

Bank routing numbers and account numbers were previously flagged `is_pii=True` with `sensitivity="high"` — a workaround that didn't capture their actual nature. They're payment credentials, regulated under PCI-DSS scope, with their own BAA-equivalent handling expectations even outside healthcare. PCI is genuinely a separate data class, not a subtype of PII.

`bank_account_type` (checking vs savings) stays `DataClass.PUBLIC`, `sensitivity="low"` — it's not sensitive on its own.

### The `first_name` PHI override pattern (preserved)

The same canonical field can have different `data_class` in different verticals. On a W-4, `first_name` is `DataClass.PII`. On a CMS-1500, `first_name` is one of HIPAA's 18 identifiers (45 CFR 164.514(b)(2)) and becomes `DataClass.PHI`. Handled by redeclaring the field on `HealthcareIntakeForm`:

```python
class HealthcareIntakeForm(IntakeFormBase):
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
```

Verbose but explicit. The alternative — a class-level decorator that flips data classification in bulk — would be cleverer but harder to reason about during a code review or audit. Verbosity wins for compliance-adjacent code.

The `_validate_canonical_names()` helper runs at import time and raises if `FieldMeta.canonical_name` doesn't match the attribute name. Catches typos that would otherwise silently break the alias join.

### `Annotated` + `FieldMeta` pattern (unchanged from original)

Three patterns were considered for attaching classification metadata to fields:

1. **Per-instance flags on `ExtractedField`** — Set classification every time you instantiate the field. Rejected: repetitive boilerplate at extraction time, and the classification is a property of the canonical field, not the value.
2. **Class-level constants** like `PHI_FIELDS = {"first_name", ...}` — Rejected: works for one model but breaks for inheritance; subclasses would need to merge sets manually.
3. **`typing.Annotated[ExtractedField[T], FieldMeta(...)]`** — Chosen. Metadata travels with the field annotation, is introspectable via `typing.get_type_hints(cls, include_extras=True)`, gets overridden cleanly when a subclass redeclares the field, and is invisible to Pydantic's serialization (so it doesn't bloat the JSON state passed through Step Functions).

The `FieldMeta` dataclass:

```python
@dataclass(frozen=True, slots=True)
class FieldMeta:
    canonical_name: str
    description: str
    data_class: DataClass = DataClass.PUBLIC
    sensitivity: Sensitivity = "low"
    source_standard: Optional[str] = None
```

---

## 4. JSON serializability for Step Functions

Step Functions passes state between Lambda invocations as JSON. Every model is `model_dump_json()`-safe and round-trips through `model_validate_json()` losslessly, validated in the test suite.

Two specific things this constrains:

- **No `bytes` fields.** Image regions get referenced by S3 URI in `metadata.source_document_id`, not embedded.
- **`date` and `datetime` use ISO-8601 strings in JSON** (Pydantic v2 default). Downstream parsers must use Pydantic's `model_validate_json`, not `json.loads` + `model_validate`, to get the conversion.

`FieldMeta` is **not** in the JSON output — it's class-level annotation metadata, not field data. Confirmed in the round-trip test.

The size impact of the four gap-fix additions on serialized JSON is small:
- `BoundingBox` model: ~80 bytes per box (vs ~40 for the previous tuple). At 30 fields per form: ~2.4 KB total.
- `PageMetadata`: ~150 bytes per page × 10 pages typical: ~1.5 KB.
- `DataClass`: same size as the previous boolean flags.
- `compute_form_confidence` output: not serialized into the form (it's computed on demand).

Total JSON payload increase per form: ~3-4 KB. Step Functions state limit is 256 KB. Plenty of margin.

---

## 5. Field choices anchored to source standards

For the alias table to seed well from the start, every canonical field name has to map to phrasings that actually appear on real forms.

### Insurance — ACORD 125 (2007/10) Commercial Insurance Application

The ACORD 125 is the foundation of every commercial submission except Workers Comp and Medical Professional Liability. Field names came from inspection of the official ACORD 125 PDF and the FirstConnect Insurance agent guide that explicitly lists the underwriting "danger zones": Named Insured, FEIN, Lines of Business, Loss History.

Notable design choice: `named_insured` is the canonical name for the legal entity, separate from the inherited `first_name` / `last_name`. On a sole-proprietor policy these are the same person; on a corporate policy `named_insured` is "Acme Widgets LLC" and `first_name` / `last_name` are the contact. Conflating them would lose the distinction.

`loss_history` is modeled as `ExtractedField[list[LossEvent]]` where `LossEvent` is itself a Pydantic model with five extracted-field children. This gives the table-row extraction the same provenance treatment as scalar fields. Note that `LossEvent`'s inner fields don't carry `FieldMeta` — see Section 8 for the deferred decision.

### Healthcare — CMS-1500 (02/12) + HIPAA 837P + patient registration patterns

CMS-1500 box numbers came from the CMS Medicare Claims Processing Manual Chapter 26 and the NUCC Reference Instruction Manual. The HIPAA 837P field IDs (e.g., `Loop 2010BA: NM109` for `insurance_member_id`) came from the Kaiser Permanente clean-claim requirements doc.

Patient registration / consent / medication intake fields came from inspection of representative real-world forms (Cornerstone Family Healthcare patient registration, Phreesia HIPAA-compliant intake form list, Jotform's hospital registration template). These aren't standardized the way ACORD or CMS-1500 are, so the field choices reflect what appears across multiple sample forms rather than a single anchor.

The `hipaa_acknowledgment` boolean is the form-side artifact of the patient receiving the Notice of Privacy Practices required under 45 CFR 164.520(c). It's modeled as a boolean (acknowledged/not) rather than capturing the full text because the text is templated by the practice — what matters for the pipeline is the binary state.

The PHI override pattern applies to all inherited base PII fields plus the healthcare-specific identifiers. The 18 HIPAA identifiers from 45 CFR 164.514(b)(2) all get `data_class=DataClass.PHI` with `sensitivity="high"`.

### HR — USCIS Form I-9 (01/20/2025) + IRS Form W-4 (2026)

I-9 fields came from the current USCIS Form I-9 PDF (edition 01/20/2025, valid through 05/31/2027) and the M-274 Handbook for Employers Section 3.0. The 2026-specific change worth noting: the Section 1 Box 4 attestation reverted from "noncitizen authorized to work" to "alien authorized to work" to match INA statutory language. The `citizenship_status` Literal uses `"alien_authorized_to_work"` to reflect this. Forms completed on the 08/01/2023 edition (still valid) will have the older "noncitizen" phrasing, which the alias table absorbs.

W-4 fields came from the IRS-released final 2026 Form W-4 PDF and Pub 15-T. The 2026 form is significant for OBBBA changes: Child Tax Credit raised to $2,200 per qualifying child (from $2,000), and the previously-write-in exemption became an explicit checkbox in a new section between Steps 4 and 5. The `dependents_credit_amount` field captures the Step 3 dollar total; the `exempt_from_withholding` boolean captures the new checkbox.

Direct deposit fields (`bank_routing_number`, `bank_account_number`, `bank_account_type`) aren't standardized to a single form — every employer has their own. Routing and account numbers are classified `DataClass.PCI` with `sensitivity="high"` because they're functionally payment credentials. `bank_account_type` (checking vs savings) is `DataClass.PUBLIC` since it's not sensitive on its own.

---

## 6. Things deliberately not in scope

Per the master architecture doc:

- **Database schema for the canonical-fields and field-aliases tables** — separate concern; the seed JSON is shaped to load into a `(canonical_name, vertical)`-keyed metadata table joined to a `(canonical_name, vertical, alias_text)` aliases table, but the DDL belongs in the Terraform module for the database.
- **Alias normalization algorithm** — the seed table feeds the algorithm but doesn't define it. Likely uses fuzzy matching (RapidFuzz) plus a small classifier; out of scope here.
- **Few-shot retrieval format** — corrections accumulate against canonical names; the retrieval layer reads them via pgvector. Format is defined in the feedback module.
- **Per-state fraud warnings, foreign operations questions, multi-job worksheet line items** — rare or compliance-boilerplate fields that bloat the schema without improving extraction quality.

---

## 7. Reproducing this artifact

```bash
cd intake_schemas/
pip install pydantic pytest
python build_alias_seed.py    # regenerates alias_table_seed.json
python -m pytest test_intake_schemas.py -v   # 40 tests, all pass
```

The seed JSON is regenerated deterministically from the schema metadata plus the hand-curated `ALIASES` map in `build_alias_seed.py`. Editing aliases means editing that dict; editing canonical fields means editing `intake_schemas.py` and regenerating.

---

## 8. Resolved deferred decisions

These were considered during schema design and during the architectural gap-fix review. Each was explicitly deferred to a future phase or to the production roadmap document. They are documented here so the reasoning isn't lost. (The SignatureCapture sub-model was originally in this section as a deferred decision; it has since been resolved and adopted — see Section 12 for the dedicated rationale.)

### Resolved: `LossEvent` inner-field PHI flagging

**Deferred to `production-roadmap.md`** as a "third-party PII handling" item.

The current `LossEvent` model has five `ExtractedField` children (occurrence_date, description, amount_paid, amount_reserved, status) without `FieldMeta` annotations. They lack data classification.

**Resolution:** For the synthetic-data demo this is fine. In production, any text-field row of a loss event likely names a third party (claimant) and is therefore PII at minimum. The production migration would:

1. Extend `LossEvent` to use the same `Annotated[ExtractedField[T], FieldMeta(...)]` pattern as the form classes
2. Add a third-party-PII sensitivity classification
3. Introduce a routing rule that requires BAA-eligible handling for any form containing third-party PII

Document, don't build. Insurance vertical is dropped from the active build per the locked architecture; this is a future-extensibility concern.

### Resolved: Spanish-language alias table extension

**Deferred to `production-roadmap.md`** as a Texas-healthcare consideration.

The alias table seeds with `(canonical_name, vertical, alias_text)` keys, no language metadata. Spanish-language intake forms are common in Texas healthcare contexts and would require a four-tuple key.

**Resolution:** Mechanical extension when needed. The schema doesn't need to change today; the alias table primary key extends from three columns to four when Spanish-language data enters scope. Phase 1-6 of the build doesn't include Spanish-language Synthea generation, so this is a non-blocker.

---

## 9. The `BoundingBox` model

The original schema represented bounding boxes as `Optional[tuple[float, float, float, float]]` — a four-element tuple of x1/y1/x2/y2 page-unit coordinates. During gap-fix review, two issues surfaced:

1. **No page number.** Multi-page forms (3-10 pages typical) need to know which page a field came from for the review UI to highlight the correct region. The tuple had no place for this.
2. **Tuples-with-typed-positions are hard to read** at the call site. `bbox[0]` could be page or x1; without context, code reviewers couldn't tell.

The fix: a typed, frozen Pydantic model.

```python
class BoundingBox(BaseModel):
    model_config = ConfigDict(frozen=True)
    page_number: int = Field(ge=1)
    x1: float
    y1: float
    x2: float
    y2: float
```

### Coordinate convention: PaddleOCR-style x1/y1/x2/y2

Two conventions are common in vision OCR: `(x1, y1, x2, y2)` for top-left and bottom-right corners (PaddleOCR, most vision-LLM outputs), and `(left, top, width, height)` (AWS Textract).

The schema uses x1/y1/x2/y2 because:
- Tier 1 (PaddleOCR-VL) and Tier 3a (Qwen 2.5 VL) both output this format natively
- Tier 2 (Textract) is the only provider that uses left/top/width/height; conversion happens in `tier2_textract.py` before returning to the canonical schema
- It's the dominant convention in the open-source vision-OCR ecosystem

### Why frozen

Bounding boxes are extraction outputs. Once a tier reports a box, downstream consumers (the review UI, the eval harness, audit logs) shouldn't mutate it. `frozen=True` enforces this at the model level — any code that tries to modify a box raises a validation error rather than silently corrupting state.

This matters for the review UI specifically. When a reviewer corrects a value, they don't change the bounding box of where the value was extracted from — they change the value. The box is part of the audit trail.

### The `page_number` requirement

Validated at `>= 1` (1-indexed to match human-facing page numbering — page numbers in citations, document viewers, and review UIs are universally 1-indexed). Required, not optional: a bounding box without a page number is meaningless on a multi-page form.

---

## 10. The `PageMetadata` model

Multi-page forms need per-page tracking. The original `FormMetadata` only had `page_count: Optional[int]`, which is insufficient: you can't tell which pages were extracted, which were blank, which failed, or which had OCR rotation correction applied.

```python
class PageMetadata(BaseModel):
    page_number: int = Field(ge=1)
    page_image_uri: str  # S3 URI of rendered page image
    page_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tier_used_for_page: Optional[TierId] = None
    page_status: Literal[
        "extracted", "skipped_blank", "failed", "manual_only"
    ] = "extracted"
    rotation_corrected: Optional[float] = None  # degrees
```

`FormMetadata` now has `pages: list[PageMetadata] = Field(default_factory=list)`. Single-page forms can omit the list entirely.

### Why `page_status` is an enum, not a boolean

The four states represent meaningfully different routing decisions:

- **`"extracted"`**: page was processed normally; field-level extraction outputs are valid.
- **`"skipped_blank"`**: page was detected as blank (e.g., separator pages, scanned-back-of-page artifacts). The routing layer should NOT escalate this page to higher tiers looking for fields that don't exist.
- **`"failed"`**: extraction encountered an unrecoverable error on this page. The page goes to the review queue with full error history; do not retry automatically.
- **`"manual_only"`**: page contains content the cascade chose not to attempt (e.g., handwritten signatures requiring human verification, or pages flagged by a pre-classifier as out-of-scope).

A boolean (`extracted: bool`) couldn't express these distinctions. The four-state enum drives meaningful routing logic.

### Why `rotation_corrected` is included

Rotation is one of the three primary OCR failure modes (rotation, low contrast, occlusion). Tracking the correction applied lets the eval harness answer "what's our F1 on rotated pages vs upright pages?" — important for understanding when the cascade should escalate vs accept.

### Deliberately excluded from `PageMetadata`

Two fields were considered and rejected:

- **`extraction_duration_ms`**: belongs in CloudWatch metrics, not in the form schema. The schema describes data; observability data goes through the observability stack.
- **`field_count_extracted`**: derivable from the form itself by counting populated `ExtractedField` instances. Storing it creates synchronization risk (what if the count is wrong?). Computing it on demand is cheap.

Both were carved out to keep the schema focused on extraction data, not telemetry.

---

## 11. The `compute_form_confidence` function

Form-level confidence aggregation is needed by three different consumers:

- **Routing layer**: uses min confidence to decide whether to escalate
- **Eval harness**: uses mean confidence to track quality over time
- **Review UI**: displays both, plus blank/unattempted counts to direct reviewer attention

To avoid three subtly different implementations, the schema provides a single helper:

```python
def compute_form_confidence(
    form: BaseModel,
    recurse: bool = True,
) -> dict[str, float | int]:
    """
    Returns:
        {
            "min": float,
            "mean": float,
            "field_count": int,
            "blank_count": int,
            "unattempted_count": int,
        }
    """
```

### Definition 1: "populated" means `value is not None`

Three definitions of "populated" were considered:

1. **`value is not None`** (chosen) — only fields with extracted values count
2. **`tier_used is not None`** — every field where extraction was attempted counts, including ones that came back blank
3. **Both `value` and `tier_used` set** — strictest: extracted AND produced a value

The chosen definition excludes confidently-blank fields from the confidence calculation. A form with 3 populated fields and 27 confidently-blank fields would, under Definition 2, have its mean confidence inflated by the 27 "I'm sure it's blank" results. The aggregate confidence is supposed to answer "how trustworthy is what was extracted?" — confident absence isn't extraction quality.

The blank fields aren't lost: `blank_count` and `unattempted_count` are returned alongside the confidence values, so the review UI can surface them as "fields requiring human attention."

### Why both `min` and `mean` are returned

`min` is what the routing layer uses for escalation decisions: one weak field should force escalation regardless of how good the rest are.

`mean` is what the eval harness uses for quality tracking: a 1% improvement in mean across batches is the F1-over-time chart's signal that the pipeline is self-improving.

Computing both costs nothing extra (single pass, two reductions) and lets each consumer use the right metric without writing its own aggregation.

### Recursion into nested models

The function walks into list-valued `ExtractedField`s containing `BaseModel` items. Concretely: an `InsuranceIntakeForm` with three `LossEvent` rows in `loss_history` will include all 15 nested `ExtractedField` confidences in the calculation.

This is correct behavior. A form with three loss events has 3x the extraction risk of a form with one. The min should reflect that. The "fairness" argument for per-LossEvent aggregation (where each loss event contributes only one min, not all five fields) was rejected — for routing purposes, you genuinely want to escalate if any single loss-event field is weak.

Recursion is unbounded (Pydantic models can't have cycles, so the depth is bounded by the schema definition). `recurse=False` is available for the eval harness when measuring top-level-only metrics.

### Why module-level, not method or computed_field

Three placements were considered:

1. **Module-level function** (chosen) — `compute_form_confidence(form)`. Stateless, called from multiple unrelated contexts, works on any model not just `IntakeFormBase` subclasses.
2. **Method on `IntakeFormBase`** — `form.compute_confidence()`. Reads naturally but couples the function to the model class hierarchy. If you ever want to call it on `LossEvent` directly, you have to duplicate the method or change the inheritance.
3. **`@computed_field`** — auto-property accessed as `form.aggregate_confidence`. Always-in-sync, but the value appears in `model_dump_json()` output, bloating Step Functions state on every transition. Step Functions has a 256 KB state limit; auto-included computed fields chip away at it.

The functional choice matches the project's stated style preference: functional over class-heavy where reasonable.

---

## 12. The `SignatureCapture` sub-model

Originally the schema represented signature presence as `signature_present: ExtractedField[bool]` — a binary "is there a signature here" answer. During architectural review of the cascade routing logic, two issues surfaced.

First, real-world intake forms arrive via two substantially different submission paths. Online PDFs typically carry typed signatures — "John Doe" in a regular font, "/s/ John Doe" in legal documents, or a typed name rendered in a script font for visual effect. In-person submissions carry handwritten ink signatures, often messy, often crossing field boundaries. Tablet-drawn signatures land somewhere between the two. The cascade has different F1 on each — Tier 1 is reliable on typed signatures and unreliable on handwritten ink. A boolean conflates them; the routing layer loses the signal needed to escalate intelligently.

Second, the boolean prevented context-aware confidence interpretation. Tier 1 reporting confidence 0.92 on a typed signature is a strong signal that the field is correct. Tier 1 reporting confidence 0.92 on a handwritten signature is suspicious because Tier 1's confidence calibration is unreliable on handwriting. The routing layer should escalate the second case despite the high reported confidence — but only if it knows which case it's in.

The fix: a typed sub-model.

```python
class SignatureCapture(BaseModel):
    model_config = ConfigDict(frozen=True)
    present: bool
    appears_handwritten: Optional[bool] = None
    appears_typed: Optional[bool] = None
```

The field on `IntakeFormBase` becomes `signature: ExtractedField[SignatureCapture]`. Confidence and bounding box live on the `ExtractedField` wrapper as before. The bounding box on the wrapper provides spatial info for the review UI; the `SignatureCapture` value provides classification info for the routing layer.

### Why `Optional[bool]` for `appears_handwritten` and `appears_typed`

Three-state classification: `True` (cascade is confident this attribute applies), `False` (cascade is confident it does not), `None` (cascade can't tell — typically because `present` is False, or because the visual signal is too weak to classify reliably).

A two-state `bool` would force the cascade to commit to a classification it might not be confident about, propagating noise into the routing layer. `None` is an honest "I don't know" that the routing layer can handle explicitly (treat as ambiguous, escalate).

### Why both `appears_handwritten` and `appears_typed` rather than a single `Literal["handwritten", "typed", "ambiguous"]`

Two separate booleans permit the both-True case for genuinely ambiguous signatures. A typed name in a heavy script font visually resembles handwriting; a tablet-drawn signature that landed cleanly looks typed. The cascade should be able to say "I see signals consistent with both classifications" rather than being forced to pick one or fall back to "ambiguous."

The routing layer treats both-True as a review-queue trigger — the form gets human attention rather than the cascade guessing. A single Literal would require defining "ambiguous" as a category, which is less expressive than letting both flags fire.

### Why frozen

Extraction outputs shouldn't mutate after the cascade produces them. Once a `SignatureCapture` is reported, downstream consumers (the review UI, eval harness, audit logs) shouldn't change it — corrections happen via separate mechanisms that produce new `ExtractedField` instances, not by mutating existing ones. `frozen=True` enforces this at the model level, matching the convention established for `BoundingBox`.

### Why `present: bool` (not Optional)

The `present` field has no `None` state because the cascade is always required to make a determination. If the signature region is genuinely unobservable (page corner cut off, severe occlusion), the routing layer should fail the extraction outright with a `tier_used="manual_only"` `PageMetadata` status — not silently report `present=None` and propagate ambiguity.

### Routing benefit

The cascade can now make context-aware escalation decisions:

- Tier 1 confidence 0.92 with `appears_typed=True`: typed signatures are reliable for Tier 1; accept.
- Tier 1 confidence 0.92 with `appears_handwritten=True`: Tier 1 confidence is unreliable on handwriting; force escalation despite high reported confidence.
- Tier 1 confidence 0.65 with `appears_handwritten=True`: expected pattern; escalate to Tier 3a where vision-LLM handles handwriting better.
- Tier 1 confidence 0.65 with `appears_typed=True`: surprising; possibly form-quality issue rather than signature ambiguity. Escalate to Tier 2 for second look.
- `present=True` with both `appears_handwritten=True` and `appears_typed=True`: genuinely ambiguous; route to review queue.

These distinctions are exactly the kind of routing nuance the cascade is designed to make — and the boolean prevented all of them.

### Synthetic data implications

The Synthea + Playwright rendering pipeline supports both signature modes — typed and handwritten — to give the cascade real diversity. The locked rendering parameters (font choices, SVG filter design, rotation jitter, distribution, reproducibility seed) live in Section 1 under "Signature rendering parameters for synthetic data generation." That subsection is the canonical specification for the Phase 3 implementer.

Briefly: ~1-2 hours of Phase 3 effort, three handwriting fonts (Caveat / Sacramento / Homemade Apple) plus Arial for typed, an SVG ink-bleed filter on handwritten only, ±3 degrees rotation jitter on handwritten only, 70% typed / 30% handwritten via seeded random check. Full specifics in Section 1.

If Phase 6 F1 measurement shows the cascade can't generalize from font-rendered handwriting to real handwriting, the upgrade path — public datasets like IAM, or a small handwriting GAN — is documented in `docs/production-roadmap.md`.

## 13. Schema scope: vertical pivot pending Phase 4

The current schema covers three verticals (Insurance, Healthcare, HR). The locked build plan calls for a vertical pivot in Phase 4:

- **Drop Insurance and HR** from the active cascade. The classes remain in code as future-extensibility examples.
- **Add `BusinessDocumentForm`** anchored to DocILE's 55-field annotation schema directly (rather than designing a separate canonical schema). DocILE provides 6,680 annotated real business documents; using their taxonomy preserves dataset compatibility and saves a research cycle.
- **Healthcare remains as-is**, with synthetic data generated by Synthea rendered onto CMS-1500-inspired templates via HTML+Playwright.

This pivot does not require changes to `IntakeFormBase`, `ExtractedField`, `BoundingBox`, `SignatureCapture`, `PageMetadata`, `compute_form_confidence`, or any of the gap-fix constructs. They were designed to be vertical-agnostic. Only the subclass roster changes.

When Phase 4 begins, the schema work will:
1. Add `BusinessDocumentForm` extending `IntakeFormBase` with DocILE's 55 fields
2. Update `build_alias_seed.py` to generate alias entries for the new vertical
3. Regenerate `alias_table_seed.json`
4. Add tests covering the new vertical
5. Re-issue this rationale document with a new section documenting the DocILE field choices (parallel to the Insurance/Healthcare/HR sections in this version)

Until that work happens, this rationale describes the schema as it currently exists.
