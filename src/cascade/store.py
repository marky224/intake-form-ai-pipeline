"""SQLite eval store for the V1 orchestrator (``data/v1.db``).

Normalized schema, three tables the orchestrator writes, two reserved for
Phase 8 so the RAG correction-feedback loop writes into the *same* file
without a migration:

- ``runs`` — one row per document processed. The run-level summary: routed
  vertical + Stage 1 score, the tier the document terminated at, the
  document's final aggregate confidence, terminal status
  (``extracted`` | ``review_queue``), wall-clock latency.
- ``field_attempts`` — one row per ``(doc_id, field_name, tier)``. A faithful
  persistent mirror of the in-memory ``ExtractedField.escalation_history``
  trail: every tier that touched a field, the value/confidence it produced,
  and why it escalated. This is the granularity Phase 6's F1-over-time and
  Phase 8's correction loop both query.
- ``review_queue`` — one row per document that exhausted Tier 3, with the
  full per-tier error history as JSON (no cloud Sonnet above Tier 3 in V1).
- ``corrections`` / ``embeddings`` — **reserved for Phase 8** (reviewer
  corrections + ColQwen 2.5 vectors). Created here so Phase 8 only adds
  writes, never a schema migration. ``embeddings.vector`` is a plain
  ``BLOB`` placeholder; Phase 8 swaps it for a ``sqlite-vec`` ``vec0``
  virtual table — out of Phase 5 scope.

Deliberately **not** in the orchestrator's write path: ``ground_truth`` and
``batch_id``. Those are Phase 6 eval-harness concepts; the orchestrator has
no ground truth at run time. The eval harness joins truth in by ``doc_id``
externally. Keeping them out avoids perpetually-NULL columns and stops two
phases sharing one table's write path.

Types are chosen to map cleanly onto the V2 Aurora ``staging`` schema:
``TEXT``→``VARCHAR``, ``REAL``→``DOUBLE PRECISION``, ``INTEGER``→``INTEGER``,
ISO-8601 UTC strings→``TIMESTAMP``. ``tier`` is stored as ``TEXT`` because
``intake_schemas.TierId`` is a mixed ``Literal[1, 2, "3a", "3b"]`` — every
tier id stringifies uniformly (``"1"``, ``"2"``, ``"3a"``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from _paths import src_root

#: Default DB location. Single file, gitignored (see ``.gitignore``).
#: Anchored at the src tree (``data/`` moved under src/ in the 2026-05-19
#: src-layout refactor — see memory project_src_layout). Was cwd-relative
#: ``Path("data/v1.db")`` which only worked when invoked from the repo root.
DEFAULT_DB_PATH = src_root() / "data" / "v1.db"

#: Terminal run states. ``extracted`` = cascade produced a form;
#: ``review_queue`` = Tier 3 exhausted, document parked for human review.
RUN_STATUS_EXTRACTED = "extracted"
RUN_STATUS_REVIEW = "review_queue"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    doc_id            TEXT PRIMARY KEY,
    vertical          TEXT NOT NULL,
    router_stage      INTEGER NOT NULL,
    router_score      REAL NOT NULL,
    final_tier        TEXT,
    final_confidence  REAL NOT NULL,
    status            TEXT NOT NULL
                      CHECK (status IN ('extracted', 'review_queue')),
    total_latency_ms  REAL NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_attempts (
    doc_id             TEXT NOT NULL,
    field_name         TEXT NOT NULL,
    tier               TEXT NOT NULL,
    value              TEXT,
    confidence         REAL NOT NULL,
    escalation_reason  TEXT,
    latency_ms         REAL NOT NULL,
    PRIMARY KEY (doc_id, field_name, tier),
    FOREIGN KEY (doc_id) REFERENCES runs (doc_id)
);

CREATE INDEX IF NOT EXISTS idx_field_attempts_doc
    ON field_attempts (doc_id);

CREATE TABLE IF NOT EXISTS review_queue (
    doc_id         TEXT PRIMARY KEY,
    error_history  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES runs (doc_id)
);

-- Reserved for Phase 8 (RAG correction-feedback loop). Created now so
-- Phase 8 adds writes, not a migration. Not written by the Phase 5
-- orchestrator.
CREATE TABLE IF NOT EXISTS corrections (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                       TEXT NOT NULL,
    field_name                   TEXT NOT NULL,
    original_value               TEXT,
    corrected_value              TEXT,
    tier_that_produced_original  TEXT,
    session_id                   TEXT,
    created_at                   TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES runs (doc_id)
);

-- Reserved for Phase 8. ``vector`` is a plain BLOB placeholder; Phase 8
-- replaces this with a sqlite-vec vec0 virtual table.
CREATE TABLE IF NOT EXISTS embeddings (
    doc_id      TEXT PRIMARY KEY,
    vector      BLOB,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES runs (doc_id)
);
"""


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (Aurora ``TIMESTAMP``-ready)."""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open ``db_path`` with foreign-key enforcement on.

    Creates the parent directory if missing. ``":memory:"`` is honored for
    tests. SQLite ships with FK enforcement *off* by default — turn it on
    per-connection so the ``field_attempts``/``review_queue`` → ``runs``
    references are real, not decorative.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table + index if absent. Idempotent."""
    conn.executescript(_SCHEMA)
    conn.commit()


def record_run(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    vertical: str,
    router_stage: int,
    router_score: float,
    final_tier: str | None,
    final_confidence: float,
    status: str,
    total_latency_ms: float,
) -> None:
    """Upsert the run-level summary row.

    ``INSERT OR REPLACE`` so re-processing a ``doc_id`` (e.g. an
    ``EVAL_LIVE`` regeneration) overwrites cleanly rather than colliding on
    the primary key. Must be called before ``record_field_attempts`` /
    ``enqueue_review`` for the same ``doc_id`` so the foreign key resolves.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO runs
          (doc_id, vertical, router_stage, router_score, final_tier,
           final_confidence, status, total_latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            vertical,
            router_stage,
            router_score,
            final_tier,
            final_confidence,
            status,
            total_latency_ms,
            _now_iso(),
        ),
    )
    conn.commit()


def record_field_attempts(
    conn: sqlite3.Connection,
    doc_id: str,
    attempts: Iterable[Mapping[str, Any]],
) -> None:
    """Bulk-upsert per-(field, tier) attempt rows.

    Each mapping carries ``field_name``, ``tier``, ``value``, ``confidence``,
    ``escalation_reason`` (None for the tier that finalized the field), and
    ``latency_ms``. ``value`` is stringified at the call site (mixed
    ExtractedField inner types collapse to text for storage; the typed value
    lives in the in-memory form). ``INSERT OR REPLACE`` keyed on
    ``(doc_id, field_name, tier)``.
    """
    rows = [
        (
            doc_id,
            a["field_name"],
            str(a["tier"]),
            a["value"],
            a["confidence"],
            a["escalation_reason"],
            a["latency_ms"],
        )
        for a in attempts
    ]
    if not rows:
        return
    conn.executemany(
        """
        INSERT OR REPLACE INTO field_attempts
          (doc_id, field_name, tier, value, confidence,
           escalation_reason, latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def enqueue_review(
    conn: sqlite3.Connection,
    doc_id: str,
    error_history: Any,
) -> None:
    """Park a Tier-3-exhausted document for human review.

    ``error_history`` is JSON-serialized verbatim — the full per-tier trail
    (which tiers ran, what they returned, why each escalated) so a reviewer
    has the complete picture with no cloud fallback above Tier 3 in V1.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO review_queue (doc_id, error_history, created_at)
        VALUES (?, ?, ?)
        """,
        (doc_id, json.dumps(error_history, default=str), _now_iso()),
    )
    conn.commit()
