"""Harness-owned eval_results / eval_batches store."""

from __future__ import annotations

import sqlite3

from cascade import store as cascade_store
from evals import store as eval_store
from evals.metrics import BatchMetrics, Counts


def _conn() -> sqlite3.Connection:
    return eval_store.connect(":memory:")


def test_eval_tables_created_alongside_cascade_tables():
    conn = _conn()
    cascade_store.init_db(conn)  # both schemas coexist in one file
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"eval_results", "eval_batches"}.issubset(tables)
    assert {"runs", "field_attempts", "review_queue"}.issubset(tables)
    # The harness never touches the orchestrator's tables.
    assert "ground_truth" not in tables and "batch_id" not in tables


def test_doc_result_roundtrip_and_stage_pk():
    conn = _conn()
    for stage in ("tier1", "cascade"):
        eval_store.record_doc_result(
            conn,
            doc_id="d1",
            batch_id=1,
            seed_version="1.0.0",
            stage=stage,
            vertical="healthcare",
            split="test",
            counts=Counts(5, 1, 1, 2),
            latency_ms=12.5,
        )
    rows = conn.execute(
        "SELECT stage, tp, blank_excluded, cost_usd FROM eval_results ORDER BY stage"
    ).fetchall()
    assert rows == [("cascade", 5, 2, 0.0), ("tier1", 5, 2, 0.0)]


def test_record_batch_and_f1_over_time_defaults_to_tier1():
    conn = _conn()
    for batch_id, f1c in ((1, Counts(2, 6, 6)), (2, Counts(6, 2, 2))):
        for stage in ("tier1", "cascade"):
            m = BatchMetrics(
                counts=f1c if stage == "tier1" else Counts(7, 1, 1),
                latency_p50_ms=10.0,
                latency_p99_ms=20.0,
                cost_per_doc_usd=0.0,
                doc_count=6,
            )
            eval_store.record_batch(
                conn,
                batch_id=batch_id,
                seed_version="1.0.0",
                git_sha="abc1234",
                stage=stage,
                metrics=m,
            )
    tier1 = eval_store.f1_over_time(conn, "1.0.0", "abc1234")  # default stage
    assert [b for b, *_ in tier1] == [1, 2]
    assert tier1[0][1] < tier1[1][1]  # climbs
    cascade = eval_store.f1_over_time(conn, "1.0.0", "abc1234", stage="cascade")
    assert all(round(f, 3) == round(Counts(7, 1, 1).f1, 3) for _, f, *_ in cascade)
