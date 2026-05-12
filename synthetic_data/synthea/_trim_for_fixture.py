"""Trim raw Synthea FHIR bundles down to the resources the parser exercises.

Synthea exports years of clinical history per patient — even at
``years_of_history=1`` an elderly patient bundle runs to several MB. The
checked-in test fixture only needs to validate ``extract_patient()``, so
each bundle is reduced to:

  * The single Patient resource
  * The 3 most recent Encounter resources (with their ``period.start``)
  * Nothing else (no Observations, Conditions, MedicationRequests, etc.)

The trimmed bundles remain valid FHIR R4 Bundles and they preserve the
exact field shape Synthea emits for Patient + Encounter, which is what
the parser cares about.

Usage::

    python -m synthetic_data.synthea._trim_for_fixture \\
        synthetic_data/output/synthea/fhir tests/fixtures/synthea/fhir
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

MAX_ENCOUNTERS_PER_BUNDLE = 3

# Fallback for missing/invalid encounter timestamps. UTC-aware so mixed
# naive/aware datetimes can't raise TypeError mid-sort when a malformed
# entry sits alongside real Synthea encounters.
_MISSING_START = datetime.min.replace(tzinfo=UTC)


def _encounter_start(entry: dict) -> datetime:
    """Sort key: parsed UTC-aware datetime, falling back to epoch-min on missing/invalid."""
    raw = entry.get("resource", {}).get("period", {}).get("start")
    if not raw:
        return _MISSING_START
    try:
        # Python 3.11+ fromisoformat handles "Z" too, but normalize defensively.
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return _MISSING_START


def _resource_type(entry: dict) -> str | None:
    resource = entry.get("resource")
    if not isinstance(resource, dict):
        return None
    return resource.get("resourceType")


def trim_bundle(bundle: dict) -> dict:
    """Reduce a Synthea FHIR Bundle to Patient + 3 most-recent Encounters."""
    entries = bundle.get("entry", [])
    patient_entries = [e for e in entries if _resource_type(e) == "Patient"]
    encounter_entries = [e for e in entries if _resource_type(e) == "Encounter"]
    encounter_entries.sort(key=_encounter_start, reverse=True)
    kept = patient_entries + encounter_entries[:MAX_ENCOUNTERS_PER_BUNDLE]
    return {**bundle, "entry": kept}


def main() -> None:
    """CLI entry point: trim every patient bundle in ``src`` into ``dst``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path, help="Synthea fhir output dir")
    parser.add_argument("dst", type=Path, help="Destination dir for trimmed fixtures")
    args = parser.parse_args()

    args.dst.mkdir(parents=True, exist_ok=True)
    written = 0
    for path in sorted(args.src.glob("*.json")):
        if path.name.startswith(("hospitalInformation", "practitionerInformation")):
            continue
        bundle = json.loads(path.read_text(encoding="utf-8"))
        trimmed = trim_bundle(bundle)
        (args.dst / path.name).write_text(
            json.dumps(trimmed, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        written += 1
    print(f"Wrote {written} trimmed bundles to {args.dst}")


if __name__ == "__main__":
    main()
