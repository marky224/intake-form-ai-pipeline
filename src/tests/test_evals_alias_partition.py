"""Progressive alias partition + the active-batch context manager."""

from __future__ import annotations

from cascade import router
from cascade.providers import tier1_paddleocr_local as tier1_mod
from evals.alias_partition import (
    active_alias_batch,
    batch_count,
    load_seed,
    partition_seed,
)


def test_batch_count_is_longest_alias_list():
    seed = load_seed()
    assert batch_count(seed) == max(len(r["aliases"]) for r in seed["fields"])


def test_partition_truncates_to_positions_0_to_n_minus_1():
    seed = load_seed()
    b1 = partition_seed(seed, 1)
    assert all(len(r["aliases"]) <= 1 for r in b1["fields"])
    b2 = partition_seed(seed, 2)
    assert all(len(r["aliases"]) <= 2 for r in b2["fields"])
    # Records shorter than N keep their full list (no padding).
    full = partition_seed(seed, batch_count(seed))
    assert [r["aliases"] for r in full["fields"]] == [r["aliases"] for r in seed["fields"]]
    # Top-level metadata preserved.
    assert b1["version"] == seed["version"]


def test_partition_seed_is_a_copy():
    seed = load_seed()
    before = list(seed["fields"][0]["aliases"])
    partition_seed(seed, 1)
    assert seed["fields"][0]["aliases"] == before  # original untouched


def test_active_alias_batch_repoints_and_restores():
    orig_router = router.ALIAS_TABLE_PATH
    orig_tier1 = tier1_mod.ALIAS_TABLE_PATH
    with active_alias_batch(1) as bn:
        assert bn == 1
        assert orig_router != router.ALIAS_TABLE_PATH
        assert tier1_mod.ALIAS_TABLE_PATH == router.ALIAS_TABLE_PATH
        # Batch 1 vocab is canonical-only — strictly smaller than the full
        # seed's vocabulary.
        b1_vocab = set(router.build_distinctive_vocabulary())
    assert orig_router == router.ALIAS_TABLE_PATH
    assert orig_tier1 == tier1_mod.ALIAS_TABLE_PATH
    full_vocab = set(router.build_distinctive_vocabulary())
    assert b1_vocab.issubset(full_vocab)
    assert len(b1_vocab) < len(full_vocab)


def test_active_alias_batch_restores_on_exception():
    orig = router.ALIAS_TABLE_PATH
    try:
        with active_alias_batch(2):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert orig == router.ALIAS_TABLE_PATH
