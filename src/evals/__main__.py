"""``python -m evals`` CLI — the ``just eval`` / ``just by-stage`` entrypoint.

``run``   — progressive-batch sweep over the ``test`` split, persisting
            per-doc + per-batch rows into ``data/v1.db`` (the shared SQLite
            file; eval tables are harness-owned), then regenerates the thin
            fixtures manifest. Default cached-replay = $0; set
            ``EVAL_LIVE=true`` for a live on-GPU run.
``by-stage`` — regenerate only the committed by-stage SVG
            (``docs/assets/f1-by-stage.svg``) from a fresh cached
            by-stage measurement (F1 triple + escalation funnel). Drift-
            guarded by ``test_evals_by_stage.py``; the "make it green again"
            command if that guard fails CI. ``--split dev`` is the held-out
            confirmation run: it reads the gitignored local corpus (or
            ``--image-dir``), prints the numbers and never rewrites the
            committed (test-split) SVG. Run it from a checkout of this code
            on the GPU box with the models up and *without* ``EVAL_LIVE``:
            the providers are cache-first, so each dev document is read
            live once on its first miss and saved, and all three stages are
            scored from that one reading. ``EVAL_LIVE=true`` would re-read
            on every pass, and it is also the only mode in which the 240 s
            per-call cap can fail a call (otherwise the over-cap reading is
            already saved and the retry replays it). The saved replay files
            land under ``tests/fixtures/eval-cache/``, which is tracked, so
            the run leaves them untracked there; keep them out of commits.
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
from pathlib import Path

from cascade.store import DEFAULT_DB_PATH
from evals.alias_partition import load_seed
from evals.by_stage import compute_by_stage, write_by_stage_chart
from evals.harness import run_eval, write_fixtures_manifest
from evals.manifest import build_corpus_manifest, load_manifest, write_manifest


def _regenerate_fixtures_manifest() -> None:
    seed_version, entries = load_manifest()
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


def _by_stage(split: str = "test", image_dir: Path | None = None) -> int:
    """Measure by-stage F1 + funnel on ``split`` (images from ``image_dir``
    when given). Only the default run — ``test`` split, committed images —
    regenerates the committed SVG; any other run just prints the numbers.

    The summary is printed, not logged: a live run loads PaddleOCR-VL, which
    can reset Python logging and swallow a logged line.
    """
    seed_version, _ = load_manifest()
    result = compute_by_stage(split=split, image_dir=image_dir)
    if split == "test" and image_dir is None:
        write_by_stage_chart(result, seed_version=seed_version)
    f = result.funnel
    (_, f1_t1), (_, f1_t12), (_, f1_t123) = result.stages
    prefix = "" if split == "test" else f"[{split} split] "
    print(
        "%sby-stage F1: %.3f → %.3f → %.3f | funnel cells T1=%d T1+2=%d "
        "T1+2+3=%d (of %d populated; %d missed, %d true negatives)"
        % (
            prefix,
            f1_t1,
            f1_t12,
            f1_t123,
            *[c for _, c in f.cumulative],
            f.populated,
            f.missed,
            f.true_negative,
        ),
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="sweep + persist to data/v1.db + regen artifacts")
    by_stage_cmd = sub.add_parser("by-stage", help="regen committed by-stage SVG only")
    by_stage_cmd.add_argument(
        "--split",
        choices=("test", "dev"),
        default="test",
        help="dev = held-out confirmation (local corpus, cache-through: no EVAL_LIVE); logs only",
    )
    by_stage_cmd.add_argument(
        "--image-dir",
        type=Path,
        default=None,
        help="folder of <doc_id>.png/.json pairs (default: committed test images, or the "
        "local corpus for --split dev); a run with this set never rewrites the SVG",
    )
    sub.add_parser(
        "build-manifest",
        help="regen committed manifest.json from the local corpus (stratified)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "build-manifest":
        return _build_manifest()

    if args.cmd == "by-stage":
        return _by_stage(args.split, args.image_dir)

    # args.cmd == "run"
    series = run_eval(db_path=DEFAULT_DB_PATH)
    _regenerate_fixtures_manifest()
    tier1, cascade = series["tier1"], series["cascade"]
    print(
        f"headline Tier-1 F1: {tier1[0][1]:.3f} (batch 1) "
        f"→ {tier1[-1][1]:.3f} (batch {tier1[-1][0]}) | "
        f"cascade F1 (robustness, flat): {cascade[-1][1]:.3f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
