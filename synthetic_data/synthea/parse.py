"""Parse Synthea FHIR Bundle output into the demographics + encounter
summary that the Phase 3 CMS-1500 renderer needs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class SyntheaPatient:
    """Demographics + most-recent encounter pulled from a Synthea FHIR bundle.

    The shape the Phase 3 CMS-1500 renderer consumes: enough to populate
    the patient-identity, address, and service-line boxes on the form
    template without re-reading the source bundle.
    """

    patient_id: str
    given_name: str
    family_name: str
    birth_date: date
    gender: str
    address_line: str
    city: str
    state: str
    postal_code: str
    phone: str | None
    most_recent_encounter_date: date | None
    most_recent_encounter_reason: str | None


def load_bundle(path: Path | str) -> dict:
    """Load a Synthea FHIR Bundle JSON file from disk."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_patient_bundles(fhir_dir: Path | str) -> list[Path]:
    """Patient bundle paths in a Synthea output dir, sorted for determinism.

    Skips ``hospitalInformation*.json`` and ``practitionerInformation*.json``
    — Synthea writes these once per run and they're not patient-shaped.
    """
    fhir_dir = Path(fhir_dir)
    return sorted(
        p
        for p in fhir_dir.glob("*.json")
        if not p.name.startswith("hospitalInformation")
        and not p.name.startswith("practitionerInformation")
    )


def _typed_resources(bundle: dict, resource_type: str) -> list[dict]:
    """Return every resource in ``bundle.entry`` with the given resourceType.

    Validates the FHIR Bundle envelope up front (``bundle`` is a dict and
    ``bundle["entry"]`` is a list) and skips malformed entries instead of
    raising opaque KeyErrors mid-iteration.
    """
    if not isinstance(bundle, dict):
        raise ValueError("Bundle must be a dict")
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        raise ValueError("Bundle must contain an 'entry' list")
    matches: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if not isinstance(resource, dict):
            continue
        if resource.get("resourceType") == resource_type:
            matches.append(resource)
    return matches


def extract_patient(bundle: dict) -> SyntheaPatient:
    """Pull the demographics + most-recent Encounter from a Synthea bundle.

    Synthea always writes exactly one Patient resource per bundle and
    typically dozens of Encounter resources spanning the patient's
    simulated medical history. Raises ``ValueError`` if the bundle is
    malformed or lacks a Patient resource.
    """
    patients = _typed_resources(bundle, "Patient")
    if not patients:
        raise ValueError("No Patient resource found in bundle")
    patient_res = patients[0]
    encounter_resources = _typed_resources(bundle, "Encounter")

    name = patient_res["name"][0]
    given = name["given"][0] if name.get("given") else ""
    family = name.get("family", "")
    birth = date.fromisoformat(patient_res["birthDate"])
    gender = patient_res.get("gender", "unknown")

    # `address` can be present-but-empty in synthetic edge cases; the
    # `or [{}]` fallback covers both missing and empty-list shapes.
    address_res = (patient_res.get("address") or [{}])[0]
    address_lines = address_res.get("line") or [""]
    address_line = address_lines[0]
    city = address_res.get("city", "")
    state = address_res.get("state", "")
    postal_code = address_res.get("postalCode", "")

    phone: str | None = None
    for tel in patient_res.get("telecom", []):
        if tel.get("system") == "phone":
            phone = tel.get("value")
            break

    most_recent_date: date | None = None
    most_recent_reason: str | None = None
    # Parse each encounter's start once, skip malformed timestamps. One
    # bad value shouldn't crash extraction for the whole bundle.
    parsed_encounters: list[tuple[dict, datetime]] = []
    for enc in encounter_resources:
        raw = enc.get("period", {}).get("start")
        if not raw:
            continue
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            parsed_encounters.append((enc, datetime.fromisoformat(raw)))
        except (TypeError, ValueError):
            continue
    if parsed_encounters:
        # Sort by parsed datetime so mixed UTC offsets (rare in Synthea
        # but possible) order correctly. Lexical ISO sort would fail there.
        parsed_encounters.sort(key=lambda pair: pair[1], reverse=True)
        latest, latest_dt = parsed_encounters[0]
        most_recent_date = latest_dt.date()
        reason_codes = latest.get("reasonCode") or []
        if reason_codes:
            rc = reason_codes[0]
            coding = rc.get("coding") or []
            if coding and coding[0].get("display"):
                most_recent_reason = coding[0]["display"]
            elif rc.get("text"):
                most_recent_reason = rc["text"]

    return SyntheaPatient(
        patient_id=patient_res["id"],
        given_name=given,
        family_name=family,
        birth_date=birth,
        gender=gender,
        address_line=address_line,
        city=city,
        state=state,
        postal_code=postal_code,
        phone=phone,
        most_recent_encounter_date=most_recent_date,
        most_recent_encounter_reason=most_recent_reason,
    )
