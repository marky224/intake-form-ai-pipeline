"""Partition manifest + the CI leakage / drift guards."""

from __future__ import annotations

import json

import pytest

from _paths import repo_root
from evals.alias_partition import load_seed
from evals.manifest import (
    CMS1500_VALIDATION_DIR,
    MANIFEST_PATH,
    ManifestEntry,
    assign_split,
    build_corpus_manifest,
    derive_partition_key,
    load_manifest,
    patient_key_from_doc_id,
    validate_partition,
)

# alias_table_seed.json stays at the **repo root** (not under src/) by
# canonical-artifact contract — see memory project_src_layout. The old
# ``MANIFEST_PATH.parent.parent`` walk worked when manifest.py lived at
# ``evals/manifest.py``; under src/ it resolved to ``src/`` instead.
ALIAS_SEED = json.loads((repo_root() / "alias_table_seed.json").read_text(encoding="utf-8"))


def test_patient_key_strips_sha8_suffix():
    assert (
        patient_key_from_doc_id("aa616abe-1761-0c9a-7959-07544679dafd-e0d8a677")
        == "aa616abe-1761-0c9a-7959-07544679dafd"
    )


def test_assign_split_deterministic_and_partitions():
    a = assign_split("patient-1")
    assert a == assign_split("patient-1")  # stable
    assert a in {"train", "dev", "test"}


def test_validate_partition_raises_on_leakage():
    leaky = [
        ManifestEntry("d1", "p1", "healthcare", "train", "a" * 64),
        ManifestEntry("d2", "p1", "healthcare", "test", "b" * 64),
    ]
    with pytest.raises(ValueError, match="leakage"):
        validate_partition(leaky)


def test_clean_partition_passes():
    clean = [
        ManifestEntry("d1", "p1", "healthcare", "train", "a" * 64),
        ManifestEntry("d2", "p2", "healthcare", "test", "b" * 64),
    ]
    validate_partition(clean)  # no raise


# --- CI guards over the committed manifest ---------------------------------


def test_committed_manifest_partition_is_leak_free():
    """Mirrors the Phase 5 router drift test: the committed partition must
    never land a patient key in more than one split."""
    _, entries = load_manifest()
    validate_partition(entries)


def test_committed_manifest_seed_version_matches_alias_seed():
    """Seed-version drift guard: the manifest's recorded seed version must
    match ``alias_table_seed.json`` so F1-over-time runs stay comparable."""
    seed_version, _ = load_manifest()
    assert seed_version == ALIAS_SEED["version"]


def test_committed_manifest_split_is_reproducible_stratification():
    """Stratification drift guard: every committed entry's split must equal
    ``assign_split`` of its derived patient key. ``manifest.json`` is the
    584-doc broad corpus (train/dev/test); the partition is a pure function
    of the patient key, so it is exactly reproducible without the gitignored
    render dir — a hand-edit that moves a doc between splits fails here."""
    _, entries = load_manifest()
    assert len(entries) > 6, "expected the broad corpus, not the legacy 6-doc slice"
    for e in entries:
        assert e.vertical == "healthcare"
        assert e.split == assign_split(derive_partition_key(e.doc_id, e.vertical))
    # All three buckets are populated (sanity that stratification ran).
    assert {e.split for e in entries} == {"train", "dev", "test"}


def test_validation_dir_is_exactly_the_test_split():
    """Lean-commit invariant: only the ``test``-split docs are staged into
    the committed validation dir (``run_eval`` iterates only ``test``;
    train/dev PNG bytes are deliberately never committed). Each staged doc
    is a (PNG, sidecar) pair."""
    _, entries = load_manifest()
    test_ids = {e.doc_id for e in entries if e.split == "test"}
    staged_png = {p.stem for p in CMS1500_VALIDATION_DIR.glob("*.png")}
    staged_json = {p.stem for p in CMS1500_VALIDATION_DIR.glob("*.json")}
    assert staged_png == test_ids
    assert staged_json == test_ids


def test_build_corpus_manifest_stratifies(tmp_path):
    """``build_corpus_manifest`` walks render-dir sidecars and assigns each
    a deterministic split (the local regen tool behind the committed file)."""
    import json as _json

    for i in range(3):
        doc_id = f"patient-{i}-deadbeef"
        (tmp_path / f"{doc_id}.json").write_text(
            _json.dumps({"image_sha256": f"{i:064d}"}), encoding="utf-8"
        )
    built = build_corpus_manifest(render_dir=tmp_path)
    assert len(built) == 3
    for b in built:
        assert b.vertical == "healthcare"
        assert b.split == assign_split(derive_partition_key(b.doc_id, "healthcare"))


def test_fixtures_manifest_doc_ids_match_manifest():
    fm = json.loads((MANIFEST_PATH.parent / "fixtures_manifest.json").read_text(encoding="utf-8"))
    _, entries = load_manifest()
    assert fm["doc_ids"] == sorted(e.doc_id for e in entries)
    assert fm["seed_version"] == load_seed()["version"]
