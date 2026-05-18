"""Partition manifest + the CI leakage / drift guards."""

from __future__ import annotations

import json

import pytest

from evals.alias_partition import load_seed
from evals.manifest import (
    MANIFEST_PATH,
    ManifestEntry,
    assign_split,
    build_cms1500_manifest,
    load_manifest,
    patient_key_from_doc_id,
    validate_partition,
)

ALIAS_SEED = json.loads(
    (MANIFEST_PATH.parent.parent / "alias_table_seed.json").read_text(encoding="utf-8")
)


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


def test_committed_manifest_matches_validation_corpus():
    """The committed manifest is exactly the on-disk CMS-1500 corpus."""
    _, entries = load_manifest()
    rebuilt = build_cms1500_manifest()
    assert sorted(e.doc_id for e in entries) == sorted(e.doc_id for e in rebuilt)
    assert all(e.split == "test" for e in entries)
    assert all(e.vertical == "healthcare" for e in entries)


def test_fixtures_manifest_doc_ids_match_manifest():
    fm = json.loads((MANIFEST_PATH.parent / "fixtures_manifest.json").read_text(encoding="utf-8"))
    _, entries = load_manifest()
    assert fm["doc_ids"] == sorted(e.doc_id for e in entries)
    assert fm["seed_version"] == load_seed()["version"]
