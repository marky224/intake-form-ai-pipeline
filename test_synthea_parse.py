"""Tests for the Synthea FHIR Bundle parser.

The fixture at ``tests/fixtures/synthea/fhir/`` is a trimmed snapshot of
Synthea v4.0.0 output with ``-p 5 -s 42 Massachusetts`` and
``years_of_history=1``. Synthea wrote 6 bundles (5 alive + 1 deceased +
replacement); the fixture preserves all six. Each bundle keeps only the
Patient resource + the 3 most recent Encounter resources — full Synthea
output is several MB per bundle and is unnecessary for parser tests. See
``synthetic_data/synthea/_trim_for_fixture.py`` for the regeneration
utility.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from synthetic_data.synthea.parse import (
    SyntheaPatient,
    extract_patient,
    find_patient_bundles,
    load_bundle,
)

FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "synthea" / "fhir"


@pytest.fixture(scope="session")
def bundle_paths() -> list[Path]:
    """Patient FHIR bundle paths discovered in the committed fixture."""
    paths = find_patient_bundles(FIXTURE_DIR)
    assert paths, f"No patient FHIR bundles found in {FIXTURE_DIR}"
    return paths


@pytest.fixture(scope="session")
def patients(bundle_paths: list[Path]) -> list[SyntheaPatient]:
    """Parsed ``SyntheaPatient`` per fixture bundle."""
    return [extract_patient(load_bundle(p)) for p in bundle_paths]


def test_fixture_has_six_bundles(bundle_paths: list[Path]) -> None:
    """Lock fixture count to catch regen drift.

    ``-p 5`` told Synthea to generate 5 alive patients; one died in
    simulation and was replaced, yielding 6 total bundles.
    """
    assert len(bundle_paths) == 6


def test_find_patient_bundles_excludes_info_files(tmp_path: Path) -> None:
    """``find_patient_bundles`` skips ``hospitalInformation``/``practitionerInformation`` files."""
    (tmp_path / "patient_abc.json").write_text("{}", encoding="utf-8")
    (tmp_path / "hospitalInformation1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "practitionerInformation1.json").write_text("{}", encoding="utf-8")
    found = find_patient_bundles(tmp_path)
    assert [p.name for p in found] == ["patient_abc.json"]


def test_each_bundle_loads_as_fhir_bundle(bundle_paths: list[Path]) -> None:
    """Every fixture file parses as a FHIR R4 Bundle with an ``entry`` array."""
    for path in bundle_paths:
        bundle = load_bundle(path)
        assert bundle["resourceType"] == "Bundle"
        assert "entry" in bundle


def test_each_patient_has_demographics(patients: list[SyntheaPatient]) -> None:
    """Every parsed patient carries the identity fields the renderer needs."""
    for pt in patients:
        assert pt.patient_id, "patient_id must be non-empty"
        assert pt.given_name, f"given_name empty for {pt.patient_id}"
        assert pt.family_name, f"family_name empty for {pt.patient_id}"
        assert isinstance(pt.birth_date, date)
        assert pt.gender in {"male", "female", "other", "unknown"}


def test_each_patient_has_massachusetts_address(patients: list[SyntheaPatient]) -> None:
    """State is the 2-letter US Core code (``MA``), per the recipe pin."""
    for pt in patients:
        assert pt.state == "MA", f"unexpected state {pt.state!r} for {pt.patient_id}"
        assert pt.city, f"city empty for {pt.patient_id}"


def test_each_patient_has_most_recent_encounter(patients: list[SyntheaPatient]) -> None:
    """Every patient surfaces at least one Encounter date.

    Synthea always generates at least one Encounter per patient within
    the export window, so the parser must find one.
    """
    for pt in patients:
        assert (
            pt.most_recent_encounter_date is not None
        ), f"{pt.patient_id} has no encounter in the trimmed fixture"
        assert isinstance(pt.most_recent_encounter_date, date)


def test_encounter_reason_may_be_none(patients: list[SyntheaPatient]) -> None:
    """Parser returns ``Optional[str]`` for ``reasonCode``-less encounters.

    Some Synthea encounters omit reasonCode (e.g., wellness visits).
    Every reason must be either None or a str — no other types leak.
    """
    reasons = [pt.most_recent_encounter_reason for pt in patients]
    assert all(r is None or isinstance(r, str) for r in reasons)


def test_most_recent_encounter_is_the_latest(bundle_paths: list[Path]) -> None:
    """``most_recent_encounter_date`` equals the max ``period.start`` per bundle."""
    for path in bundle_paths:
        bundle = load_bundle(path)
        encounters = [
            e["resource"]
            for e in bundle["entry"]
            if e["resource"]["resourceType"] == "Encounter"
            and e["resource"].get("period", {}).get("start")
        ]
        if not encounters:
            continue
        truth = max(datetime.fromisoformat(e["period"]["start"]) for e in encounters).date()
        parsed = extract_patient(bundle).most_recent_encounter_date
        assert parsed == truth, f"{path.name}: parsed {parsed} != truth {truth}"
