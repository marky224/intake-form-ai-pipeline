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

    # Headline: Tier-1 F1 climbs sharply from canonical-only (batch 1), then
    # asymptotes to a plateau. At the broad 92-doc scale the plateau has a
    # sub-0.001 non-monotone wobble at the alias-saturation tail (batch 2 is
    # the exact peak; later batches sit a hair below) — honest behavior, not
    # smoothed into a strictly-monotone curve.
    t1 = [f for _, f in series["tier1"]]
    assert t1[0] < t1[-1], "Tier-1 F1 must improve with alias coverage"
    assert t1[-1] - t1[0] > 0.05, "the climb must be non-trivial"
    assert max(t1) - t1[-1] < 0.005, "tail must plateau near the peak"

    # Robustness: end-to-end cascade F1 is invariant to alias coverage
    # (strong Tier 2/3 escalation compensates) — the documented finding.
    casc = [f for _, f in series["cascade"]]
    assert len(set(round(f, 6) for f in casc)) == 1

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
