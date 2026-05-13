"""Integration tests for the Playwright CMS-1500 renderer.

These tests launch headless Chromium and are marked ``slow`` so CI
(which does not install the Chromium binary) skips them. Run locally
with ``uv run pytest -m slow test_render.py``.

The renderer's pure-Python logic (signature generation, sidecar shape)
is also covered by ``test_signature.py`` without needing Chromium.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthetic_data.render.config import PAGE_HEIGHT_PX, PAGE_WIDTH_PX
from synthetic_data.render.render import (
    SIDECAR_SCHEMA_VERSION,
    render_one,
)
from synthetic_data.render.signature import patient_signature
from synthetic_data.synthea.parse import extract_patient, find_patient_bundles, load_bundle

pytestmark = pytest.mark.slow

FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "synthea" / "fhir"


@pytest.fixture(scope="module")
def first_patient():
    paths = find_patient_bundles(FIXTURE_DIR)
    assert paths, f"No patient bundles in {FIXTURE_DIR}"
    return extract_patient(load_bundle(paths[0]))


def test_render_produces_png_and_sidecar(first_patient, tmp_path: Path) -> None:
    png_path, sidecar_path = render_one(first_patient, tmp_path)
    assert png_path.exists()
    assert sidecar_path.exists()
    # PNG should be non-trivial (a blank page would be ~5 KB; a real
    # rendered form is north of 30 KB even at 100 DPI).
    assert png_path.stat().st_size > 10_000, f"suspiciously small PNG: {png_path.stat().st_size}B"


def test_sidecar_json_shape(first_patient, tmp_path: Path) -> None:
    _, sidecar_path = render_one(first_patient, tmp_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert sidecar["schema_version"] == SIDECAR_SCHEMA_VERSION
    assert sidecar["source_id"] == first_patient.patient_id
    assert sidecar["image"].endswith(".png")
    assert isinstance(sidecar["image_sha256"], str) and len(sidecar["image_sha256"]) == 64

    page = sidecar["page"]
    assert page == {"number": 1, "width_px": PAGE_WIDTH_PX, "height_px": PAGE_HEIGHT_PX}


def test_sidecar_signature_matches_signature_module(first_patient, tmp_path: Path) -> None:
    """The sidecar's recorded signature equals what signature.py would
    independently generate for the same patient id — verifies the
    renderer didn't accidentally re-seed or re-roll."""
    _, sidecar_path = render_one(first_patient, tmp_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    full_name = f"{first_patient.given_name} {first_patient.family_name}".strip()
    expected = patient_signature(first_patient.patient_id, full_name)
    assert sidecar["signature"]["mode"] == expected.mode
    assert sidecar["signature"]["font"] == expected.font
    assert sidecar["signature"]["rotation_deg"] == round(expected.rotation_deg, 3)


def test_sidecar_field_bboxes_are_within_page(first_patient, tmp_path: Path) -> None:
    """Every extracted bbox sits inside the page bounds — basic sanity check."""
    _, sidecar_path = render_one(first_patient, tmp_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert sidecar["fields"], "expected at least one field bbox to be extracted"
    for field in sidecar["fields"]:
        bbox = field["bbox"]
        assert 0 <= bbox["x1"] < bbox["x2"] <= PAGE_WIDTH_PX, f"x out of bounds: {field}"
        assert 0 <= bbox["y1"] < bbox["y2"] <= PAGE_HEIGHT_PX, f"y out of bounds: {field}"
        assert field["name"], "field must have a name"
        assert field["cms1500_box"], "field must reference a CMS-1500 box"


def test_sidecar_contains_expected_field_names(first_patient, tmp_path: Path) -> None:
    """Every field the template marks with data-field appears in the sidecar."""
    _, sidecar_path = render_one(first_patient, tmp_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    names = {f["name"] for f in sidecar["fields"]}
    expected_minimal = {
        "patient_name",
        "patient_birth_date",
        "patient_address_line",
        "patient_city",
        "patient_state",
        "patient_postal_code",
        "signature",
        "diagnosis",
    }
    missing = expected_minimal - names
    assert not missing, f"missing fields in sidecar: {missing}"


def test_rendered_value_contains_patient_data(first_patient, tmp_path: Path) -> None:
    """The patient_name field's text content in the sidecar reflects
    the SyntheaPatient input — confirms the template variables wired
    through to the rendered DOM."""
    _, sidecar_path = render_one(first_patient, tmp_path)
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    name_field = next(f for f in sidecar["fields"] if f["name"] == "patient_name")
    assert first_patient.family_name in name_field["value"]
    assert first_patient.given_name in name_field["value"]
