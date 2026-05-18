"""``python -m evals`` CLI — the ``just eval`` / ``just chart`` entrypoint.

``run``   — progressive-batch sweep over the ``test`` split, persisting
            per-doc + per-batch rows into ``data/v1.db`` (the shared SQLite
            file; eval tables are harness-owned), then regenerates the
            committed F1-over-time SVG + thin fixtures manifest from the
            Tier-1 (headline) series. Default cached-replay = $0; set
            ``EVAL_LIVE=true`` for a live on-GPU run.
``chart``  — regenerate only the committed SVG + fixtures manifest from a
            fresh cached Tier-1 sweep (no persistent DB write). The drift
            guard in ``test_evals_chart.py`` fails CI if the committed SVG
            is stale, so this is the "make it green again" command.
``build-manifest`` — regenerate the committed ``manifest.json`` from the
            gitignored full local corpus (``CORPUS_RENDER_DIR``), patient-
            stratified train/dev/test. Local-only (the corpus is not in
            CI); run after ``just synthetic-data-render-500``. Only the
            ``test`` split is staged + committed; the full partition is
            recorded so the leakage guard and Phase 9 ``train`` see it.
"""

from __future__ import annotations

import argparse
import collections
import logging
import sys
import tempfile

from cascade.store import DEFAULT_DB_PATH
from evals.alias_partition import load_seed
from evals.chart import write_chart
from evals.harness import run_eval, write_fixtures_manifest
from evals.manifest import build_corpus_manifest, load_manifest, write_manifest


def _regenerate_artifacts(series_tier1: list[tuple[int, float]]) -> None:
    seed_version, entries = load_manifest()
    write_chart(series_tier1, seed_version=seed_version)
    write_fixtures_manifest(seed_version=seed_version, entries=entries)


def _build_manifest() -> int:
    """Regenerate the committed stratified manifest from the local corpus."""
    seed_version = load_seed()["version"]
    entries = build_corpus_manifest()
    write_manifest(entries, seed_version=seed_version)
    dist = collections.Counter(e.split for e in entries)
    logging.info(
        "manifest.json regenerated: %d entries (seed v%s) — train=%d dev=%d test=%d",
        len(entries),
        seed_version,
        dist["train"],
        dist["dev"],
        dist["test"],
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="sweep + persist to data/v1.db + regen artifacts")
    sub.add_parser("chart", help="regen committed SVG + fixtures manifest only")
    sub.add_parser(
        "build-manifest",
        help="regen committed manifest.json from the local corpus (stratified)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "build-manifest":
        return _build_manifest()

    if args.cmd == "run":
        series = run_eval(db_path=DEFAULT_DB_PATH)
    else:  # chart
        series = run_eval(db_path=tempfile.mktemp(suffix=".db"))

    _regenerate_artifacts(series["tier1"])
    tier1, cascade = series["tier1"], series["cascade"]
    print(
        f"headline Tier-1 F1: {tier1[0][1]:.3f} (batch 1) "
        f"→ {tier1[-1][1]:.3f} (batch {tier1[-1][0]}) | "
        f"cascade F1 (robustness, flat): {cascade[-1][1]:.3f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
