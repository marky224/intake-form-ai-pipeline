"""End-to-end batch rendering tests across all 6 Synthea fixture bundles.

Marked ``slow`` because they launch headless Chromium. CI skips them.
Run locally with ``uv run pytest -m slow test_render_batch.py``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from synthetic_data.render.batch import main as batch_main
from synthetic_data.render.config import (
    HANDWRITTEN_FONTS,
    PAGE_HEIGHT_PX,
    PAGE_WIDTH_PX,
    TYPED_FONT,
)
from synthetic_data.render.render import _render_stem, render_batch
from synthetic_data.synthea.parse import (
    extract_patient,
    find_patient_bundles,
    load_bundle,
)

pytestmark = pytest.mark.slow

FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "synthea" / "fhir"


@pytest.fixture(scope="module")
def all_six_patients():
    paths = find_patient_bundles(FIXTURE_DIR)
    assert len(paths) == 6, f"expected 6 fixture bundles, got {len(paths)}"
    return [extract_patient(load_bundle(p)) for p in paths]


@pytest.fixture(scope="module")
def batch_output(tmp_path_factory, all_six_patients) -> Path:
    """Render all 6 fixtures once; module-scoped so the Chromium launch
    cost (~2 s) doesn't dominate per-test."""
    out = tmp_path_factory.mktemp("renderer-six")
    render_batch(all_six_patients, out)
    return out


def test_batch_produces_one_png_and_one_sidecar_per_patient(
    batch_output: Path, all_six_patients
) -> None:
    pngs = sorted(batch_output.glob("*.png"))
    jsons = sorted(batch_output.glob("*.json"))
    assert len(pngs) == len(all_six_patients) == 6
    assert len(jsons) == len(all_six_patients) == 6

    # PNG and sidecar stems match 1:1 via _render_stem(patient_id) —
    # sanitized stem + 8-char sha256 disambiguator.
    png_stems = {p.stem for p in pngs}
    json_stems = {p.stem for p in jsons}
    expected_stems = {_render_stem(p.patient_id) for p in all_six_patients}
    assert png_stems == json_stems == expected_stems


def test_batch_sidecars_round_trip_as_json(batch_output: Path) -> None:
    for sidecar_path in batch_output.glob("*.json"):
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert data["page"]["width_px"] == PAGE_WIDTH_PX
        assert data["page"]["height_px"] == PAGE_HEIGHT_PX
        assert data["signature"]["mode"] in {"typed", "handwritten"}
        assert isinstance(data["fields"], list) and data["fields"]


def test_batch_signature_modes_cover_both_typed_and_handwritten(
    batch_output: Path,
) -> None:
    """Across 6 random patient_ids at p=0.7 typed, expect at least one
    typed AND at least one handwritten in 95%+ of seeds. Binomial:
    P(all 6 typed) = 0.7^6 ~= 0.118; P(all 6 handwritten) = 0.3^6 ~=
    0.0007. Total prob of monomodal corpus ~= 0.119 — too flaky for a
    test gate. So instead we just confirm modes/fonts are well-formed
    (no None, valid family). The 70/30 distribution check at scale
    lives in test_signature.py."""
    modes_seen: Counter[str] = Counter()
    fonts_seen: set[str] = set()
    for sidecar_path in batch_output.glob("*.json"):
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sig = data["signature"]
        modes_seen[sig["mode"]] += 1
        fonts_seen.add(sig["font"])
        if sig["mode"] == "typed":
            assert sig["font"] == TYPED_FONT
            assert sig["rotation_deg"] == 0.0
        else:
            assert sig["font"] in HANDWRITTEN_FONTS
            assert -3.0 <= sig["rotation_deg"] <= 3.0

    assert sum(modes_seen.values()) == 6


def test_cli_main_renders_with_limit(tmp_path: Path) -> None:
    """The CLI entry point honors --limit and exits 0 on success."""
    out = tmp_path / "cli-out"
    rc = batch_main(
        [
            "--input",
            str(FIXTURE_DIR),
            "--output",
            str(out),
            "--limit",
            "2",
        ]
    )
    assert rc == 0
    assert len(list(out.glob("*.png"))) == 2
    assert len(list(out.glob("*.json"))) == 2


def test_cli_main_returns_nonzero_on_empty_input(tmp_path: Path) -> None:
    """If the input dir has no patient bundles, CLI reports a clean
    failure (exit code 1) instead of crashing."""
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "out"
    rc = batch_main(["--input", str(empty), "--output", str(out)])
    assert rc == 1


@pytest.mark.parametrize("bad_limit", ["0", "-1", "-5"])
def test_cli_main_rejects_non_positive_limit(bad_limit: str, tmp_path: Path) -> None:
    """Non-positive --limit values must be rejected with exit code 2
    rather than slipping through to negative-slice semantics (-N renders
    all-but-last-N) or the empty-bundles path (0 looks like nothing
    matched the input dir)."""
    rc = batch_main(
        [
            "--input",
            str(FIXTURE_DIR),
            "--output",
            str(tmp_path),
            "--limit",
            bad_limit,
        ]
    )
    assert rc == 2
    # No output files should have been created.
    assert not list(tmp_path.glob("*.png"))
    assert not list(tmp_path.glob("*.json"))
