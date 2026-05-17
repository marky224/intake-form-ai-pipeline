"""Tests for ``cascade.store`` — the V1 SQLite eval store.

Pins the normalized schema decided at Phase 5 entry: ``runs`` +
``field_attempts`` + ``review_queue`` written by the orchestrator;
``corrections`` + ``embeddings`` reserved (created, not written) for
Phase 8. Foreign keys are real, the status CHECK is enforced, and upserts
are idempotent (an ``EVAL_LIVE`` re-run overwrites cleanly).
"""

from __future__ import annotations

import sqlite3

import pytest

from cascade import store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def test_init_db_idempotent_and_tables_present(conn):
    store.init_db(conn)  # second call must not raise
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "runs",
        "field_attempts",
        "review_queue",
        "corrections",
        "embeddings",
    } <= names


def test_foreign_keys_enforced(conn):
    """A field_attempt without its parent run is rejected (PRAGMA on)."""
    with pytest.raises(sqlite3.IntegrityError):
        store.record_field_attempts(
            conn,
            "no-such-run",
            [
                {
                    "field_name": "first_name",
                    "tier": 1,
                    "value": "x",
                    "confidence": 0.9,
                    "escalation_reason": None,
                    "latency_ms": 1.0,
                }
            ],
        )


def test_record_run_and_attempts_roundtrip(conn):
    store.record_run(
        conn,
        doc_id="d1",
        vertical="healthcare",
        router_stage=1,
        router_score=5.5,
        final_tier="2",
        final_confidence=0.91,
        status=store.RUN_STATUS_EXTRACTED,
        total_latency_ms=123.4,
    )
    store.record_field_attempts(
        conn,
        "d1",
        [
            {
                "field_name": "first_name",
                "tier": 1,
                "value": "Jane",
                "confidence": 0.4,
                "escalation_reason": "low_confidence",
                "latency_ms": 2.0,
            },
            {
                "field_name": "first_name",
                "tier": 2,
                "value": "Jane",
                "confidence": 1.0,
                "escalation_reason": None,
                "latency_ms": 5.0,
            },
        ],
    )
    row = conn.execute("SELECT vertical, final_tier, status FROM runs WHERE doc_id='d1'").fetchone()
    assert row == ("healthcare", "2", "extracted")
    attempts = conn.execute(
        "SELECT tier FROM field_attempts WHERE doc_id='d1' ORDER BY tier"
    ).fetchall()
    assert [a[0] for a in attempts] == ["1", "2"]  # tier stored as TEXT


def test_record_run_upsert_overwrites(conn):
    for tier in ("1", "3a"):
        store.record_run(
            conn,
            doc_id="d1",
            vertical="healthcare",
            router_stage=1,
            router_score=1.0,
            final_tier=tier,
            final_confidence=0.5,
            status=store.RUN_STATUS_EXTRACTED,
            total_latency_ms=1.0,
        )
    rows = conn.execute("SELECT final_tier FROM runs WHERE doc_id='d1'").fetchall()
    assert rows == [("3a",)]  # one row, last write wins


def test_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.record_run(
            conn,
            doc_id="bad",
            vertical="healthcare",
            router_stage=1,
            router_score=1.0,
            final_tier="1",
            final_confidence=0.5,
            status="not_a_real_status",
            total_latency_ms=1.0,
        )


def test_enqueue_review_serializes_history(conn):
    store.record_run(
        conn,
        doc_id="d2",
        vertical="healthcare",
        router_stage=2,
        router_score=0.0,
        final_tier="3a",
        final_confidence=0.3,
        status=store.RUN_STATUS_REVIEW,
        total_latency_ms=9.0,
    )
    store.enqueue_review(conn, "d2", [{"tier": "3a", "error": "exhausted", "fields": ["dob"]}])
    raw = conn.execute("SELECT error_history FROM review_queue WHERE doc_id='d2'").fetchone()[0]
    assert '"tier": "3a"' in raw and "exhausted" in raw


def test_empty_attempts_is_noop(conn):
    store.record_run(
        conn,
        doc_id="d3",
        vertical="business",
        router_stage=1,
        router_score=1.0,
        final_tier="1",
        final_confidence=1.0,
        status=store.RUN_STATUS_EXTRACTED,
        total_latency_ms=1.0,
    )
    store.record_field_attempts(conn, "d3", [])  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM field_attempts WHERE doc_id='d3'").fetchone()[0] == 0
