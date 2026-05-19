"""Tests for ``rag.aliases`` — the live correction-driven alias overlay.

The load-bearing invariants: the committed seed is never mutated; the
overlay unions into *both* alias consumers; and the progressive-partition
sweep suppresses the overlay so the frozen F1 chart only ever sees the
seed.
"""

from __future__ import annotations

from pathlib import Path

from cascade import router
from cascade.providers import tier1_paddleocr_local as tier1_mod
from evals.alias_partition import active_alias_batch
from intake_schemas import HealthcareIntakeForm
from rag import aliases


def _fresh_overlay(tmp_path: Path):
    """Repoint the overlay at an empty temp file + clear alias caches."""
    overlay = tmp_path / "corrections_aliases.json"
    ctx = aliases.temporary_overlay(overlay)
    ctx.__enter__()
    aliases.invalidate_alias_caches()
    return overlay, ctx


def test_append_dedupes_against_overlay_and_seed(tmp_path: Path) -> None:
    _, ctx = _fresh_overlay(tmp_path)
    try:
        assert aliases.append_correction_alias("first_name", "base", "Pt First Nm") is True
        # Exact + case-insensitive duplicate in the overlay → no-op.
        assert aliases.append_correction_alias("first_name", "base", "Pt First Nm") is False
        assert aliases.append_correction_alias("first_name", "base", "  pt first nm ") is False
        # Blank → no-op.
        assert aliases.append_correction_alias("first_name", "base", "   ") is False
        # A phrasing the committed seed already covers → no-op (no phantom).
        assert aliases.append_correction_alias("address_apt", "base", "Apt") is False
    finally:
        ctx.__exit__(None, None, None)
        aliases.invalidate_alias_caches()


def test_overlay_unions_into_tier1_alias_map(tmp_path: Path) -> None:
    _, ctx = _fresh_overlay(tmp_path)
    try:
        aliases.append_correction_alias("first_name", "base", "Pt First Nm")
        aliases.invalidate_alias_caches()
        amap = tier1_mod._alias_map_for_form(HealthcareIntakeForm)
        assert "Pt First Nm" in amap["first_name"]
    finally:
        ctx.__exit__(None, None, None)
        aliases.invalidate_alias_caches()


def test_overlay_unions_into_router_distinctive_vocab(tmp_path: Path) -> None:
    _, ctx = _fresh_overlay(tmp_path)
    try:
        aliases.append_correction_alias("patient_id", "healthcare", "Zzq Unique Mrn Label")
        aliases.invalidate_alias_caches()
        vocab = router.build_distinctive_vocabulary()
        assert "ZZQ UNIQUE MRN LABEL" in vocab
    finally:
        ctx.__exit__(None, None, None)
        aliases.invalidate_alias_caches()


def test_partition_sweep_suppresses_overlay(tmp_path: Path) -> None:
    """The frozen-chart sweep must not see runtime corrections."""
    _, ctx = _fresh_overlay(tmp_path)
    try:
        aliases.append_correction_alias("patient_id", "healthcare", "Zzq Unique Mrn Label")
        aliases.invalidate_alias_caches()
        assert "ZZQ UNIQUE MRN LABEL" in router.build_distinctive_vocabulary()

        with active_alias_batch(99):  # full seed, overlay suppressed
            assert aliases.overlay_records() == []
            assert "ZZQ UNIQUE MRN LABEL" not in router.build_distinctive_vocabulary()

        # Suppression lifts on exit; the overlay is visible again.
        assert "ZZQ UNIQUE MRN LABEL" in router.build_distinctive_vocabulary()
    finally:
        ctx.__exit__(None, None, None)
        aliases.invalidate_alias_caches()


def test_overlay_records_empty_when_no_file(tmp_path: Path) -> None:
    with aliases.temporary_overlay(tmp_path / "nope.json"):
        assert aliases.overlay_records() == []


def test_committed_seed_file_is_never_written(tmp_path: Path) -> None:
    """A correction must never touch the real ``alias_table_seed.json``."""
    seed = Path(router.ALIAS_TABLE_PATH)
    before = seed.read_bytes()
    _, ctx = _fresh_overlay(tmp_path)
    try:
        aliases.append_correction_alias("first_name", "base", "Brand New Phrasing")
    finally:
        ctx.__exit__(None, None, None)
        aliases.invalidate_alias_caches()
    assert seed.read_bytes() == before
