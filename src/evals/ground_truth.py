"""Ground truth: sidecar/KILE → schema-projected, type-canonicalized values.

The eval harness scores an assembled form against ground truth at the
*schema-field* level. Two corpora, two sources of truth:

**CMS-1500 (healthcare, committed, CI).** The renderer sidecars
(``tests/fixtures/eval-validation/cms1500/<id>.json``) store one entry per
CMS-1500 *box*, and the box text is the *rendered* text — box 2 is
``"LAST, FIRST"``, box 3 packs ``MM/DD/YYYY <nbsp> SEX``. A VL model returns
clean per-field values. Scoring the raw box text against a clean extraction
would count the renderer's layout as extraction error. ``CMS1500_PROJECTION``
therefore *projects* boxes onto schema fields (splitting box-packed boxes)
and every value — truth and extracted alike — is run through the same
type-aware canonicalizer (``_canon_*``) so F1 measures extraction quality.

One sidecar box has no schema home and is **excluded** from scoring,
documented here rather than silently dropped:

- ``date_of_current_illness`` (CMS-1500 box 14) — no corresponding
  ``HealthcareIntakeForm`` field. Scoring it would require inventing a
  schema field; excluding it keeps the scored set faithful to the schema.

``signature`` (box 12) maps to ``HealthcareIntakeForm.signature``, whose
inner type is ``SignatureCapture`` (present/handwritten/typed booleans, no
text). It is **presence-scored**: truth is "a signature was rendered"
(always true for these sidecars), extracted is ``SignatureCapture.present``.

**DocILE (business, local-only, CC-BY-NC-ND).** ``BusinessDocumentForm``'s
field names are the upstream KILE fieldtypes verbatim, so a ``DocileField``'s
``fieldtype`` *is* the schema field name. Truth is the first non-empty
``text`` per fieldtype, canonicalized as free text. No DocILE-derived
artifact is committed; callers gate this path on local file presence.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from intake_schemas import SignatureCapture

if TYPE_CHECKING:
    from synthetic_data.docile.parse import DocileDocument

#: Schema field name → canonicalizer kind. Drives both truth projection and
#: reading the extracted value so the two sides are always compared in the
#: same normal form.
FIELD_KIND: dict[str, str] = {
    "first_name": "str",
    "last_name": "str",
    "address_street": "str",
    "address_city": "str",
    "address_state": "str",
    "address_zip": "str",
    "phone": "str",
    "reason_for_visit": "str",
    "date_of_birth": "date",
    "date_signed": "date",
    "sex": "sex",
    "signature": "signature",
}

#: CMS-1500 sidecar boxes with no ``HealthcareIntakeForm`` field. Excluded
#: from scoring (documented in the module docstring), asserted in tests so a
#: future schema field that adopts the box is a deliberate, visible change.
EXCLUDED_CMS1500_BOXES: frozenset[str] = frozenset({"date_of_current_illness"})

_WS_RE = re.compile(r"\s+")
_SEX_VALUES = frozenset({"M", "F", "U"})

#: A ground truth maps schema field name → canonical (already-normalized)
#: string token. Only fields with a present truth value appear; a field
#: absent here has no ground truth and is not scored.
GroundTruth = dict[str, str]


def _canon_str(value: Any) -> str | None:
    """Casefold + NFKC + whitespace-collapse. ``None``/empty → ``None``."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = _WS_RE.sub(" ", text).strip().casefold()
    return text or None


def _canon_date(value: Any) -> str | None:
    """Any supported date spelling → ``YYYY-MM-DD``. Unparseable → ``None``.

    Accepts a ``datetime.date`` (the extracted ``ExtractedField[date]``
    value) and the sidecar's ``MM/DD/YYYY`` plus ISO ``YYYY-MM-DD`` strings.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = unicodedata.normalize("NFKC", str(value)).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _canon_sex(value: Any) -> str | None:
    """First ``M``/``F``/``U`` token (case-insensitive). Else ``None``."""
    if value is None:
        return None
    token = unicodedata.normalize("NFKC", str(value)).strip().upper()[:1]
    return token if token in _SEX_VALUES else None


def _canon_signature(value: Any) -> str | None:
    """Presence token. Truth side passes a bool; extracted passes the
    ``SignatureCapture`` (or its ``present`` flag). ``None`` → not populated.
    """
    if value is None:
        return None
    if isinstance(value, SignatureCapture):
        return "present" if value.present else "absent"
    return "present" if bool(value) else "absent"


_CANON = {
    "str": _canon_str,
    "date": _canon_date,
    "sex": _canon_sex,
    "signature": _canon_signature,
}


def canonicalize(field_name: str, value: Any) -> str | None:
    """Canonical comparable token for ``value`` under ``field_name``'s kind.

    Both ground truth and the extracted form value pass through here, so a
    match is exact-string on the canonical form. Returns ``None`` when the
    value is absent/unparseable (the caller treats that as "not populated").
    Fields outside ``FIELD_KIND`` fall back to free-text string canon.
    """
    return _CANON.get(FIELD_KIND.get(field_name, "str"), _canon_str)(value)


def _split_name_last_first(box2: str) -> tuple[str | None, str | None]:
    """CMS-1500 box 2 is ``"LAST, FIRST"`` (Synthea family, given)."""
    if "," not in box2:
        return None, None
    last, _, first = box2.partition(",")
    return last.strip() or None, first.strip() or None


def _split_dob_sex(box3: str) -> tuple[str | None, str | None]:
    """CMS-1500 box 3 packs ``MM/DD/YYYY`` then the sex char (NBSP-padded)."""
    tokens = _WS_RE.sub(" ", unicodedata.normalize("NFKC", box3)).split()
    if not tokens:
        return None, None
    return tokens[0], (tokens[-1] if tokens[-1].upper() in _SEX_VALUES else None)


def load_cms1500_ground_truth(sidecar: dict[str, Any] | Path | str) -> GroundTruth:
    """Project a CMS-1500 sidecar onto canonicalized schema-field truth.

    Box-packed boxes are split (name→first/last, DOB+sex→two fields);
    ``date_of_current_illness`` is excluded; ``signature`` becomes a
    presence token (a signature is always rendered on these forms).
    """
    if isinstance(sidecar, Path | str):
        sidecar = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    raw = {
        f["name"]: ("" if f.get("value") is None else str(f["value"])) for f in sidecar["fields"]
    }

    projected: dict[str, Any] = {}
    if "patient_name" in raw:
        last, first = _split_name_last_first(raw["patient_name"])
        projected["last_name"], projected["first_name"] = last, first
    if "patient_birth_date" in raw:
        dob, sex = _split_dob_sex(raw["patient_birth_date"])
        projected["date_of_birth"], projected["sex"] = dob, sex
    projected["address_street"] = raw.get("patient_address_line")
    projected["address_city"] = raw.get("patient_city")
    projected["address_state"] = raw.get("patient_state")
    projected["address_zip"] = raw.get("patient_postal_code")
    projected["phone"] = raw.get("patient_phone")
    projected["reason_for_visit"] = raw.get("diagnosis")
    projected["date_signed"] = raw.get("date_signed")
    if "signature" in raw:
        projected["signature"] = True  # a signature is rendered on every form

    truth: GroundTruth = {}
    for field_name, value in projected.items():
        token = canonicalize(field_name, value)
        if token is not None:
            truth[field_name] = token
    return truth


def load_docile_ground_truth(doc: DocileDocument) -> GroundTruth:
    """KILE annotations → canonicalized ``BusinessDocumentForm`` truth.

    ``DocileField.fieldtype`` is the schema field name verbatim (the schema
    was generated from the upstream KILE fieldtypes). First non-empty
    ``text`` per fieldtype wins. Local-only — no committed DocILE artifact;
    callers gate on file presence.
    """
    from intake_schemas import BusinessDocumentForm

    valid = set(BusinessDocumentForm.model_fields) - {"metadata"}
    truth: GroundTruth = {}
    for f in doc.fields:
        if f.fieldtype not in valid or f.fieldtype in truth:
            continue
        token = canonicalize(f.fieldtype, f.text)
        if token is not None:
            truth[f.fieldtype] = token
    return truth


def extracted_token(field_name: str, form: Any) -> str | None:
    """Canonical token for ``form.<field_name>``'s extracted value.

    ``None`` when the field is unpopulated (value ``None``) — the caller
    distinguishes that from a populated-but-wrong value for TP/FP/FN.
    Confidently-blank vs unattempted is the caller's concern (it reads
    ``tier_used``); this only canonicalizes the value.
    """
    ef = getattr(form, field_name, None)
    if ef is None or getattr(ef, "value", None) is None:
        return None
    return canonicalize(field_name, ef.value)
