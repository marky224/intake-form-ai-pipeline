"""Harness-owned eval persistence (same ``data/v1.db`` file).

``cascade.store`` deliberately keeps ``ground_truth`` and ``batch_id`` out
of the orchestrator's write path — "those are Phase 6 eval-harness
concepts; the eval harness joins truth in by ``doc_id`` externally." This
module honors that boundary: it writes its **own** tables into the same
SQLite file, never touching the orchestrator's ``runs`` / ``field_attempts``
schema. Phase 8's correction loop can later join corrections to either side
by ``doc_id``.

Two tables:

- ``eval_results`` — one row per ``(doc_id, batch_id, seed_version, stage)``:
  the per-document TP/FP/FN + latency. The drill-down granularity Phase 8
  and any per-field diagnostics query.
- ``eval_batches`` — one row per ``(batch_id, seed_version, git_sha, stage)``:
  the micro-averaged batch metrics the F1-over-time chart reads directly.
  Micro-F1 is recomputed from summed counts, not averaged per-doc F1, so
  the batch number matches ``docs/eval-methodology.md``.

``stage`` is ``"tier1"`` or ``"cascade"``. The Phase-6 finding: end-to-end
``"cascade"`` F1 is *invariant* to alias coverage (strong Tier 2/3
escalation recovers whatever the alias layer missed), so the headline
F1-over-time chart plots the ``"tier1"`` stage — the layer the alias table
actually governs, which genuinely climbs then asymptotes. ``"cascade"`` is
persisted alongside as the robustness stat.

Types map onto the V2 Aurora ``eval`` schema the same way ``cascade.store``
documents (``TEXT``→``VARCHAR``, ``REAL``→``DOUBLE PRECISION``).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from cascade.store import DEFAULT_DB_PATH
from evals.metrics import BatchMetrics, Counts

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_results (
    doc_id         TEXT NOT NULL,
    batch_id       INTEGER NOT NULL,
    seed_version   TEXT NOT NULL,
    stage          TEXT NOT NULL CHECK (stage IN ('tier1', 'cascade')),
    vertical       TEXT NOT NULL,
    split          TEXT NOT NULL,
    tp             INTEGER NOT NULL,
    fp             INTEGER NOT NULL,
    fn             INTEGER NOT NULL,
    blank_excluded INTEGER NOT NULL,
    latency_ms     REAL NOT NULL,
    cost_usd       REAL NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (doc_id, batch_id, seed_version, stage)
);

CREATE INDEX IF NOT EXISTS idx_eval_results_batch
    ON eval_results (seed_version, stage, batch_id);

CREATE TABLE IF NOT EXISTS eval_batches (
    batch_id          INTEGER NOT NULL,
    seed_version      TEXT NOT NULL,
    git_sha           TEXT NOT NULL,
    stage             TEXT NOT NULL CHECK (stage IN ('tier1', 'cascade')),
    doc_count         INTEGER NOT NULL,
    tp                INTEGER NOT NULL,
    fp                INTEGER NOT NULL,
    fn                INTEGER NOT NULL,
    precision         REAL NOT NULL,
    recall            REAL NOT NULL,
    f1                REAL NOT NULL,
    latency_p50_ms    REAL NOT NULL,
    latency_p99_ms    REAL NOT NULL,
    cost_per_doc_usd  REAL NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (batch_id, seed_version, git_sha, stage)
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the shared DB file and ensure the eval tables exist."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the eval tables if absent. Idempotent (shared-conn callers)."""
    conn.executescript(_SCHEMA)
    conn.commit()


def record_doc_result(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    batch_id: int,
    seed_version: str,
    stage: str,
    vertical: str,
    split: str,
    counts: Counts,
    latency_ms: float,
    cost_usd: float = 0.0,
) -> None:
    """Upsert one per-document eval row (``INSERT OR REPLACE``)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO eval_results
          (doc_id, batch_id, seed_version, stage, vertical, split, tp, fp, fn,
           blank_excluded, latency_ms, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            batch_id,
            seed_version,
            stage,
            vertical,
            split,
            counts.tp,
            counts.fp,
            counts.fn,
            counts.blank_excluded,
            latency_ms,
            cost_usd,
            _now_iso(),
        ),
    )
    conn.commit()


def record_batch(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    seed_version: str,
    git_sha: str,
    stage: str,
    metrics: BatchMetrics,
) -> None:
    """Upsert the micro-averaged batch summary the chart reads."""
    c = metrics.counts
    conn.execute(
        """
        INSERT OR REPLACE INTO eval_batches
          (batch_id, seed_version, git_sha, stage, doc_count, tp, fp, fn,
           precision, recall, f1, latency_p50_ms, latency_p99_ms,
           cost_per_doc_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            seed_version,
            git_sha,
            stage,
            metrics.doc_count,
            c.tp,
            c.fp,
            c.fn,
            c.precision,
            c.recall,
            c.f1,
            metrics.latency_p50_ms,
            metrics.latency_p99_ms,
            metrics.cost_per_doc_usd,
            _now_iso(),
        ),
    )
    conn.commit()


def f1_over_time(
    conn: sqlite3.Connection,
    seed_version: str,
    git_sha: str,
    stage: str = "tier1",
) -> list[tuple[int, float, float, float]]:
    """``[(batch_id, f1, latency_p50_ms, cost_per_doc_usd), ...]`` ascending.

    Reads the batch summaries for one ``(seed_version, git_sha, stage)``
    run. Defaults to ``stage="tier1"`` — the headline F1-over-time series
    (the layer the alias table governs); pass ``"cascade"`` for the
    end-to-end robustness series.
    """
    rows = conn.execute(
        """
        SELECT batch_id, f1, latency_p50_ms, cost_per_doc_usd
          FROM eval_batches
         WHERE seed_version = ? AND git_sha = ? AND stage = ?
         ORDER BY batch_id ASC
        """,
        (seed_version, git_sha, stage),
    ).fetchall()
    return [(int(b), float(f), float(lat), float(cost)) for b, f, lat, cost in rows]
