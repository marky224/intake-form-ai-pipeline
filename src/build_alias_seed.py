"""
Generates alias_table_seed.json from the schema metadata + a hand-curated
alias map. Run from this directory:

    python build_alias_seed.py

The output JSON is a list of records, one per (canonical_name, vertical),
ready to load into Postgres tables:

    canonical_fields(canonical_name, vertical, description, data_class,
                     sensitivity, source_standard, data_type)
    field_aliases(canonical_name, vertical, alias_text)

Aliases were sourced from:
    - ACORD 125 (2007/10) Commercial Insurance Application
    - CMS-1500 (02/12) NUCC reference instruction manual
    - HIPAA 837P transaction set field labels
    - USCIS Form I-9 (01/20/2025 edition) and M-274 employer handbook
    - IRS Form W-4 (2026 final) and Pub 15-T
    - Sample patient registration / HIPAA consent / direct deposit forms

Alias priority convention (load-bearing for the F1-over-time chart):

Within each record's `aliases` list, position is meaningful. Position 0 is
the canonical/authoritative phrasing for that field (typically the exact
label from the source standard, e.g. "First Name" from USCIS Form I-9).
Subsequent positions are variants in rough decreasing real-world frequency.

The eval harness consumes this ordering for the F1-over-time chart's
progressive alias-table partition: batch N of the chart includes positions
0..N-1 of every record's aliases. See the comment block above ALIASES for
the editing rules and `docs/eval-methodology.md` for the partition strategy.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, get_args, get_origin

from _paths import repo_root
from intake_schemas import (
    HealthcareIntakeForm,
    HRIntakeForm,
    InsuranceIntakeForm,
    IntakeFormBase,
    field_metadata_as_dict,
    get_field_metadata,
)

# ---------------------------------------------------------------------------
# Hand-curated alias map.
#
# Aliases are the actual phrasings that appear on real forms. Pulled from
# inspection of the source standards listed in the module docstring.
#
# Keying convention: aliases[(canonical_name, vertical)] -> list[str]
# When a base field has the same aliases regardless of vertical, only the
# "base" entry is provided; the generator fans it out automatically.
#
# *** PRIORITY CONVENTION — read before editing ***
#
# Position within each list is load-bearing for the eval harness:
#   - position 0   : canonical/authoritative phrasing (e.g. the exact label
#                    from the source standard — "First Name" on USCIS I-9,
#                    "Patient's Date of Birth" on CMS-1500, etc.)
#   - position 1+  : progressively less-canonical variants, ordered by
#                    rough decreasing real-world frequency
#
# The F1-over-time chart's progressive alias-table partition uses this
# ordering: batch N of the chart includes positions 0..N-1 of every
# record's aliases. Reordering an existing list shifts the historical
# chart shape. Consequences for editing:
#
#   - APPEND new aliases (Phase 8 reviewer corrections, new schema work)
#     to the END of the relevant list. They represent the latest-discovered
#     variants and naturally belong in the highest-N batches.
#   - DO NOT insert in the middle or reorder existing entries unless you
#     intend to bump the seed version (currently "1.0.0"). A reorder is
#     a v2.0.0 change because it changes the v1.0.0 historical chart.
#   - Adding a new (canonical_name, vertical) record is fine at any time
#     — new records just join the partition at all batches simultaneously.
#
# The seed `version` is recorded in evals/fixtures_manifest.json entries.
# F1-over-time runs are reproducible against a frozen seed version; cross-
# version comparisons require regenerating the chart from scratch. See
# docs/eval-methodology.md for the full partition strategy.
# ---------------------------------------------------------------------------

ALIASES: dict[tuple[str, str], list[str]] = {
    # ===== BASE fields (common to all verticals) =====
    ("first_name", "base"): [
        "First Name",
        "Given Name",
        "First",
        "Firstname",
        "First Name (Given Name)",
    ],
    ("middle_name", "base"): [
        "Middle Name",
        "MI",
        "Middle Initial",
        "M.I.",
        "Middle",
    ],
    ("last_name", "base"): [
        "Last Name",
        "Family Name",
        "Surname",
        "Last",
        "Last Name (Family Name)",
    ],
    ("address_street", "base"): [
        "Street Address",
        "Address",
        "Mailing Address",
        "Street",
        "Address Line 1",
        "Street Number and Name",
        "Residential Address",
        "Home Address",
    ],
    ("address_apt", "base"): [
        "Apt",
        "Apt #",
        "Apt Number",
        "Apt. Number",
        "Suite",
        "Unit",
        "Address Line 2",
        "Apt Number (if any)",
    ],
    ("address_city", "base"): [
        "City",
        "City or Town",
        "Town",
    ],
    ("address_state", "base"): [
        "State",
        "ST",
        "State/Province",
    ],
    ("address_zip", "base"): [
        "ZIP",
        "ZIP Code",
        "Zip Code",
        "Zip",
        "Postal Code",
        "ZIP/Postal Code",
    ],
    ("date_of_birth", "base"): [
        "Date of Birth",
        "DOB",
        "Birth Date",
        "Born On",
        "D.O.B.",
        "Date of birth (mm/dd/yyyy)",
        "Birthdate",
    ],
    ("phone", "base"): [
        "Phone",
        "Phone Number",
        "Telephone",
        "Tel",
        "Phone (Home)",
        "Day Phone",
        "Cell Phone",
        "Mobile",
        "Contact Number",
    ],
    ("email", "base"): [
        "Email",
        "E-mail",
        "Email Address",
        "E-Mail",
        "Email Address (optional)",
    ],
    ("signature_present", "base"): [
        "Signature",
        "Sign Here",
        "Signature of Employee",
        "Patient Signature",
        "Applicant's Signature",
        "Signature of Patient or Authorized Representative",
    ],
    ("date_signed", "base"): [
        "Date",
        "Date Signed",
        "Signature Date",
        "Today's Date",
        "Date (mm/dd/yyyy)",
    ],
    # ===== INSURANCE-specific =====
    ("named_insured", "insurance"): [
        "Named Insured",
        "Applicant",
        "Applicant Name",
        "Insured Name",
        "First Named Insured",
        "Business Name",
        "Legal Entity Name",
        "Insured (First Named Insured)",
    ],
    ("dba_name", "insurance"): [
        "DBA",
        "Doing Business As",
        "DBA Name",
        "d/b/a",
        "Trade Name",
    ],
    ("fein", "insurance"): [
        "FEIN",
        "Federal Employer ID",
        "Tax ID",
        "EIN",
        "Federal Tax ID",
        "Federal Employer Identification Number",
        "TIN",
        "Tax Identification Number",
    ],
    ("entity_type", "insurance"): [
        "Entity Type",
        "Business Type",
        "Legal Entity",
        "Form of Business",
        "Entity Structure",
        "Business Entity",
    ],
    ("naics_code", "insurance"): [
        "NAICS",
        "NAICS Code",
        "Industry Code",
        "SIC Code",
        "SIC",
    ],
    ("business_description", "insurance"): [
        "Description of Operations",
        "Description of Primary Operations",
        "Nature of Business",
        "Business Description",
        "Primary Operations",
    ],
    ("date_business_started", "insurance"): [
        "Date Business Started",
        "Year Started",
        "Date Established",
        "Inception Date",
        "Year Founded",
        "Business Start Date",
    ],
    ("effective_date", "insurance"): [
        "Effective Date",
        "Policy Effective Date",
        "Coverage Effective",
        "Inception",
        "From",
    ],
    ("expiration_date", "insurance"): [
        "Expiration Date",
        "Policy Expiration",
        "Expires",
        "End Date",
        "To",
    ],
    ("lines_of_business", "insurance"): [
        "Lines of Business",
        "LOB",
        "Coverage Type",
        "Lines of Coverage",
        "Policy Type",
        "Coverages Requested",
    ],
    ("general_aggregate_limit", "insurance"): [
        "General Aggregate",
        "GA Limit",
        "Aggregate Limit",
        "General Aggregate Limit",
    ],
    ("each_occurrence_limit", "insurance"): [
        "Each Occurrence",
        "Per Occurrence",
        "Each Occurrence Limit",
        "Occurrence Limit",
    ],
    ("deductible", "insurance"): [
        "Deductible",
        "Deductible Amount",
        "SIR",
        "Self-Insured Retention",
        "Deductibles",
    ],
    ("premium", "insurance"): [
        "Premium",
        "Total Premium",
        "Annual Premium",
        "Estimated Premium",
        "Premiums",
    ],
    ("producer_name", "insurance"): [
        "Producer",
        "Producer's Name",
        "Agent Name",
        "Broker Name",
        "Agency",
        "Producer Name (Please Print)",
    ],
    ("producer_license_number", "insurance"): [
        "Producer License No",
        "License Number",
        "State Producer License No",
        "NPN",
        "National Producer Number",
        "Producer License Number",
    ],
    ("prior_carrier", "insurance"): [
        "Prior Carrier",
        "Prior Insurance Carrier",
        "Previous Carrier",
        "Current Carrier",
        "Expiring Carrier",
    ],
    ("loss_history", "insurance"): [
        "Loss History",
        "Claims History",
        "Prior Losses",
        "Loss Information",
        "Five Year Loss History",
    ],
    # Insurance also overrides certain base fields with insurance-specific aliases
    ("first_name", "insurance"): [
        "First Name",
        "Given Name",
        "Contact First Name",
        "Applicant First Name",
    ],
    ("last_name", "insurance"): [
        "Last Name",
        "Family Name",
        "Contact Last Name",
        "Applicant Last Name",
    ],
    # ===== HEALTHCARE-specific =====
    ("patient_id", "healthcare"): [
        "Patient ID",
        "MRN",
        "Medical Record Number",
        "Chart Number",
        "Patient Account #",
        "Account Number",
        "Patient Number",
        "Chart #",
    ],
    ("sex", "healthcare"): [
        "Sex",
        "Gender",
        "Sex Assigned at Birth",
        "M/F",
        "Male/Female",
        "Sex (M/F)",
    ],
    ("insurance_member_id", "healthcare"): [
        "Member ID",
        "Subscriber ID",
        "Insured's ID Number",
        "Policy ID",
        "ID Number",
        "Member Number",
        "Insured ID #",
    ],
    ("insurance_group_number", "healthcare"): [
        "Group Number",
        "Group #",
        "Plan Group",
        "Group ID",
        "Insurance Group Number",
        "Insured's Group or FECA Number",
    ],
    ("insurance_plan_name", "healthcare"): [
        "Insurance Plan",
        "Plan Name",
        "Insurance Provider",
        "Carrier",
        "Insurance Company",
        "Health Plan",
        "Insurance Plan Name or Program Name",
    ],
    ("subscriber_name", "healthcare"): [
        "Insured's Name",
        "Subscriber Name",
        "Policyholder Name",
        "Primary Insured",
        "Insured Name (Last Name, First Name, Middle Initial)",
    ],
    ("subscriber_relationship", "healthcare"): [
        "Patient Relationship to Insured",
        "Relationship to Subscriber",
        "Relationship",
        "Patient's Relationship to Insured",
        "Self/Spouse/Child/Other",
    ],
    ("subscriber_dob", "healthcare"): [
        "Insured's Date of Birth",
        "Subscriber DOB",
        "Policyholder DOB",
        "Insured's DOB",
    ],
    ("primary_care_physician", "healthcare"): [
        "Primary Care Physician",
        "PCP",
        "Primary Doctor",
        "Family Doctor",
        "Referring Physician",
        "Name of Referring Provider or Other Source",
        "Referring Provider",
    ],
    ("reason_for_visit", "healthcare"): [
        "Reason for Visit",
        "Chief Complaint",
        "Reason for Today's Visit",
        "Presenting Problem",
        "CC",
    ],
    ("allergies", "healthcare"): [
        "Allergies",
        "Drug Allergies",
        "Known Allergies",
        "Allergy List",
        "NKDA",
        "Allergies (please list)",
    ],
    ("current_medications", "healthcare"): [
        "Current Medications",
        "Medications",
        "Prescription Medications",
        "Meds",
        "Active Medications",
        "Prior Medications",
        "List of Current Medications",
    ],
    ("medical_history_conditions", "healthcare"): [
        "Medical History",
        "Past Medical History",
        "PMH",
        "Health Conditions",
        "Active Problems",
        "Have you ever been diagnosed with",
    ],
    ("emergency_contact_name", "healthcare"): [
        "Emergency Contact",
        "Emergency Contact Name",
        "In Case of Emergency",
        "ICE Contact",
        "Emergency Contact Person",
    ],
    ("emergency_contact_phone", "healthcare"): [
        "Emergency Contact Phone",
        "Emergency Phone",
        "ICE Phone",
        "Emergency Contact Number",
    ],
    ("emergency_contact_relationship", "healthcare"): [
        "Relationship to Patient",
        "Emergency Contact Relationship",
        "Relation",
        "Relationship",
    ],
    ("pharmacy_preference", "healthcare"): [
        "Preferred Pharmacy",
        "Pharmacy",
        "Drug Store",
        "Pharmacy Name and Phone",
        "Pharmacy Name",
        "Preferred Pharmacy Name and Address",
    ],
    ("hipaa_acknowledgment", "healthcare"): [
        "HIPAA Acknowledgment",
        "Notice of Privacy Practices",
        "Privacy Notice Received",
        "HIPAA Consent",
        "Acknowledgment of Receipt of Notice of Privacy Practices",
    ],
    # Healthcare overrides for base fields, with patient-specific phrasings
    ("first_name", "healthcare"): [
        "Patient's First Name",
        "Patient First Name",
        "First Name",
        "Given Name",
    ],
    ("last_name", "healthcare"): [
        "Patient's Last Name",
        "Patient Last Name",
        "Last Name",
        "Family Name",
        "Patient's Name (Last)",
    ],
    ("middle_name", "healthcare"): [
        "Patient's Middle Initial",
        "Middle Initial",
        "MI",
        "M.I.",
    ],
    ("date_of_birth", "healthcare"): [
        "Patient's Date of Birth",
        "Patient DOB",
        "DOB",
        "Date of Birth",
        "Birth Date",
    ],
    # ===== HR / EMPLOYMENT-specific =====
    ("ssn", "hr"): [
        "SSN",
        "Social Security Number",
        "Social Security #",
        "U.S. Social Security Number",
        "Soc Sec No",
        "Social Security No.",
    ],
    ("other_last_names_used", "hr"): [
        "Other Last Names Used",
        "Maiden Name",
        "Other Names",
        "Previous Names",
        "Other Last Names Used (if any)",
    ],
    ("citizenship_status", "hr"): [
        "Citizenship",
        "Citizenship Status",
        "Citizen of the United States",
        "Attestation",
        "Immigration Status",
        "Check one of the following boxes to attest to your citizenship or immigration status",
    ],
    ("uscis_a_number", "hr"): [
        "A-Number",
        "USCIS Number",
        "Alien Registration Number",
        "USCIS A-Number",
        "USCIS Number/A-Number",
        "Alien Number",
    ],
    ("i94_admission_number", "hr"): [
        "Form I-94",
        "I-94 Number",
        "Admission Number",
        "I-94 Admission Number",
        "Form I-94 Admission Number",
    ],
    ("foreign_passport_number", "hr"): [
        "Foreign Passport",
        "Passport Number",
        "Foreign Passport Number",
        "Foreign Passport Number and Country of Issuance",
    ],
    ("work_authorization_expiration", "hr"): [
        "Work Authorization Expiration",
        "Employment Authorization Expiration",
        "Authorized to Work Until",
        "Expiration Date (if any)",
    ],
    ("employee_start_date", "hr"): [
        "First Day of Employment",
        "Hire Date",
        "Start Date",
        "Employment Start Date",
        "Date of Hire",
        "The employee's first day of employment (mm/dd/yyyy)",
    ],
    ("employer_name", "hr"): [
        "Employer",
        "Employer Name",
        "Company Name",
        "Business Name",
        "Employer's Business or Organization Name",
    ],
    ("filing_status", "hr"): [
        "Filing Status",
        "Marital Status",
        "Tax Filing Status",
        "Single or Married filing separately",
        "Married filing jointly",
        "Head of household",
    ],
    ("multiple_jobs_indicator", "hr"): [
        "Multiple Jobs",
        "Two Jobs",
        "Step 2 Box",
        "Multiple Jobs Worksheet",
        "Multiple Jobs or Spouse Works",
    ],
    ("dependents_credit_amount", "hr"): [
        "Dependents",
        "Credit for Dependents",
        "Number of Dependents",
        "Step 3",
        "Total Dependent Credits",
        "Claim Dependents",
        "Total credit amount",
    ],
    ("other_income", "hr"): [
        "Other Income",
        "Step 4(a)",
        "Other Estimated Income",
        "Non-Wage Income",
        "Other Income (not from jobs)",
    ],
    ("deductions", "hr"): [
        "Deductions",
        "Step 4(b)",
        "Other Deductions",
        "Itemized Deductions",
    ],
    ("extra_withholding", "hr"): [
        "Extra Withholding",
        "Step 4(c)",
        "Additional Withholding",
        "Additional Amount Withheld",
        "Extra withholding per pay period",
    ],
    ("exempt_from_withholding", "hr"): [
        "Exempt",
        "Exempt from Withholding",
        "Exemption from Withholding",
        "I claim exemption from withholding",
    ],
    ("bank_routing_number", "hr"): [
        "Routing Number",
        "ABA Routing Number",
        "Bank Routing",
        "ABA Number",
        "Routing/Transit Number",
        "9-digit Routing Number",
    ],
    ("bank_account_number", "hr"): [
        "Account Number",
        "Bank Account Number",
        "Account #",
        "Checking/Savings Account Number",
    ],
    ("bank_account_type", "hr"): [
        "Account Type",
        "Checking",
        "Savings",
        "Type of Account",
        "Checking or Savings",
    ],
    # HR overrides for base fields
    ("first_name", "hr"): [
        "First Name (Given Name)",
        "First Name",
        "Employee First Name",
        "Given Name",
    ],
    ("last_name", "hr"): [
        "Last Name (Family Name)",
        "Last Name",
        "Employee Last Name",
        "Family Name",
        "Surname",
    ],
    ("middle_name", "hr"): [
        "Middle Initial (if any)",
        "Middle Initial",
        "MI",
        "M.I.",
    ],
    ("date_of_birth", "hr"): [
        "Date of Birth (mm/dd/yyyy)",
        "Date of Birth",
        "DOB",
        "Birth Date",
    ],
    ("address_apt", "hr"): [
        "Apt. Number (if any)",
        "Apt",
        "Apt #",
        "Suite",
        "Unit",
    ],
}


def _python_type_name(annotation: Any) -> str:
    """Best-effort serialization of the inner T from ExtractedField[T].

    For Pydantic v2 generic models the type parameter lives in
    `__pydantic_generic_metadata__["args"]`, not in `typing.get_args(...)`.
    """
    args = get_args(annotation)
    if not args:
        return "unknown"
    wrapper = args[0]  # e.g. ExtractedField[str]

    # Try Pydantic v2 generic metadata first.
    pyd_meta = getattr(wrapper, "__pydantic_generic_metadata__", None)
    if pyd_meta and pyd_meta.get("args"):
        t = pyd_meta["args"][0]
    else:
        inner = get_args(wrapper)
        if not inner:
            return wrapper.__name__ if hasattr(wrapper, "__name__") else str(wrapper)
        t = inner[0]

    # Render Literal["a", "b"] cleanly; render list[str] as "list[str]"; etc.
    if get_origin(t) is not None:
        return str(t).replace("typing.", "")
    return t.__name__ if hasattr(t, "__name__") else str(t)


def build_seed() -> dict[str, Any]:
    """Build the full seed payload."""
    classes = {
        "base": IntakeFormBase,
        "insurance": InsuranceIntakeForm,
        "healthcare": HealthcareIntakeForm,
        "hr": HRIntakeForm,
    }

    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # Base fields are emitted as vertical="base" only when no vertical-specific
    # alias override is provided. If a subclass overrides a base field's
    # metadata (as healthcare does), emit it under that vertical too.

    base_field_names = set(get_field_metadata(IntakeFormBase).keys())

    for vertical, cls in classes.items():
        from typing import get_type_hints

        hints = get_type_hints(cls, include_extras=True)
        cls_meta = get_field_metadata(cls)

        for canonical_name, meta in cls_meta.items():
            # Only emit a base field under vertical="base" once
            if canonical_name in base_field_names and vertical != "base":
                # Check if subclass has its own alias entry OR overrides the FieldMeta
                base_meta = get_field_metadata(IntakeFormBase)[canonical_name]
                has_specific_aliases = (canonical_name, vertical) in ALIASES
                meta_differs = (
                    meta.data_class != base_meta.data_class
                    or meta.sensitivity != base_meta.sensitivity
                    or meta.description != base_meta.description
                    or meta.source_standard != base_meta.source_standard
                )
                if not (has_specific_aliases or meta_differs):
                    continue  # Inherits as-is from base

            key = (canonical_name, vertical)
            if key in seen:
                continue
            seen.add(key)

            aliases = ALIASES.get(key)
            if aliases is None and vertical == "base":
                aliases = []
            if aliases is None:
                # Subclass override with metadata change but no specific aliases:
                # fall back to base aliases
                aliases = ALIASES.get((canonical_name, "base"), [])

            record: dict[str, Any] = {
                "canonical_name": canonical_name,
                "vertical": vertical,
                "data_type": _python_type_name(hints[canonical_name]),
                **field_metadata_as_dict(meta),
                "aliases": aliases,
            }
            # Drop the canonical_name from FieldMeta's dict (already at top)
            record.pop("canonical_name", None)
            record["canonical_name"] = canonical_name  # re-add at top for readability

            fields.append(record)

    # Sort: base first, then by vertical, then by canonical_name, for human review
    vertical_order = {"base": 0, "insurance": 1, "healthcare": 2, "hr": 3}
    fields.sort(key=lambda r: (vertical_order[r["vertical"]], r["canonical_name"]))

    return {
        "version": "1.0.0",
        "generated_at": str(date.today()),
        "schema_module": "intake_schemas",
        "description": (
            "Canonical-name -> alias-variations seed for the intake-form pipeline. "
            "Load into a Postgres `field_aliases` table keyed on (canonical_name, vertical, alias_text). "
            "Use `canonical_fields` for the metadata side of the join."
        ),
        "fields": fields,
    }


def main() -> None:
    payload = build_seed()
    # alias_table_seed.json deliberately lives at the repo root, NOT under
    # src/ (canonical-artifact contract; tests open it cwd-relative from
    # repo root). See memory project_src_layout.
    out_path = repo_root() / "alias_table_seed.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n")
    print(f"Wrote {out_path}")
    print(f"  Total field records: {len(payload['fields'])}")
    print(f"  Total aliases:       {sum(len(f['aliases']) for f in payload['fields'])}")
    by_vertical: dict[str, int] = {}
    for f in payload["fields"]:
        by_vertical[f["vertical"]] = by_vertical.get(f["vertical"], 0) + 1
    for v, n in by_vertical.items():
        print(f"  {v:12s}: {n} fields")


if __name__ == "__main__":
    main()
