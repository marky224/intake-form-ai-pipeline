"""End-to-end harness: cached-replay sweep over the 92-doc CMS-1500 test split.

Exercises the same code as a live run; the default cached-replay path keeps
it $0 and deterministic in CI (no Ollama / Paddle).
"""

from __future__ import annotations

import tempfile

from evals import store as eval_store
from evals.harness import run_eval
from evals.manifest import load_manifest


def test_full_sweep_two_stages_persisted_and_tier1_climbs():
    db = tempfile.mktemp(suffix=".db")
    series = run_eval(db_path=db)

    assert set(series) == {"tier1", "cascade"}
    seed_version, entries = load_manifest()
    # run_eval sweeps the test split only (train/dev are held out); the
    # manifest now carries all 584 docs, not just the test slice.
    n_docs = sum(1 for e in entries if e.split == "test")
    n_batches = len(series["tier1"])
    assert n_batches >= 8  # ~8-9 given the current seed

    # Headline: Tier-1 F1 climbs from canonical-only (batch 1) as more of
    # the form's labels match (0.479 → 0.865 on the test split since the
    # 2026-10-24 parser fixes); the seed-1.1.0 CMS-1500 box labels enter at
    # batches 6 and 9. Measured as is, not smoothed.
    t1 = [f for _, f in series["tier1"]]
    assert t1[0] < t1[-1], "Tier-1 F1 must improve with alias coverage"
    assert t1[-1] - t1[0] > 0.05, "the climb must be non-trivial"
    assert max(t1) - t1[-1] < 0.005, "tail must plateau near the peak"

    # Robustness: end-to-end cascade F1 barely moves with alias coverage
    # (Tier 2/3 recover most of what the alias layer misses). Since
    # 2026-10-24 Tier 1 finalizes validated values, so the cascade gains a
    # little as more labels match (0.856 → 0.868 on the test split) instead
    # of being exactly flat.
    casc = [f for _, f in series["cascade"]]
    assert casc[0] <= casc[-1]
    assert max(casc) - min(casc) < 0.02

    # Both stages persisted for every (doc, batch).
    conn = eval_store.connect(db)
    try:
        n_rows = conn.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0]
        assert n_rows == n_docs * n_batches * 2  # tier1 + cascade
        n_batch_rows = conn.execute(
            "SELECT COUNT(*) FROM eval_batches WHERE stage='tier1'"
        ).fetchone()[0]
        assert n_batch_rows == n_batches
        # Cost is $0 throughout V1.
        assert conn.execute("SELECT MAX(cost_usd) FROM eval_results").fetchone()[0] == 0.0
    finally:
        conn.close()
