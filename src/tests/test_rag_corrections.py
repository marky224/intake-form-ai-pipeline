"""Tests for ``rag.corrections`` — the feedback-loop primitives."""

from __future__ import annotations

import pytest

from cascade import store
from rag import aliases
from rag.corrections import (
    CorrectionOutcome,
    bootstrap_alias_table,
    count_corrections,
    humanize_field_label,
    record_correction,
    refresh_embedding,
    seed_vertical_for,
)


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    store.record_run(
        c,
        doc_id="doc-1",
        vertical="healthcare",
        router_stage=1,
        router_score=5.0,
        final_tier="3a",
        final_confidence=0.5,
        status="review_queue",
        total_latency_ms=1.0,
    )
    yield c
    c.close()


def test_seed_vertical_mapping() -> None:
    assert seed_vertical_for("healthcare") == "healthcare"
    assert seed_vertical_for("business") == "base"
    assert seed_vertical_for("anything-else") == "base"


def test_humanize_field_label() -> None:
    assert humanize_field_label("patient_name") == "Patient Name"
    assert humanize_field_label("date_of_birth") == "Date Of Birth"


def test_record_correction_logs_row_without_learning_alias(conn, tmp_path) -> None:
    with aliases.temporary_overlay(tmp_path / "ov.json"):
        out = record_correction(
            conn,
            doc_id="doc-1",
            field_name="date_of_birth",
            original_value="1/2/2020",
            corrected_value="2020-01-02",
            vertical="healthcare",
            tier_that_produced_original="3a",
        )
    assert isinstance(out, CorrectionOutcome)
    assert out.correction_id == 1
    assert out.alias_learned is False and out.learned_alias is None
    assert count_corrections(conn) == 1
    row = conn.execute("SELECT doc_id, field_name, corrected_value FROM corrections").fetchone()
    assert row == ("doc-1", "date_of_birth", "2020-01-02")


def test_record_correction_learns_new_phrasing(conn, tmp_path) -> None:
    with aliases.temporary_overlay(tmp_path / "ov.json"):
        out = record_correction(
            conn,
            doc_id="doc-1",
            field_name="first_name",
            original_value=None,
            corrected_value="Jane",
            vertical="healthcare",
            label_phrasing="Pt Given Nm",
        )
        assert out.alias_learned is True
        assert out.learned_alias == "Pt Given Nm"
        # A second identical correction does not re-learn it.
        out2 = record_correction(
            conn,
            doc_id="doc-1",
            field_name="first_name",
            original_value=None,
            corrected_value="Jane",
            vertical="healthcare",
            label_phrasing="Pt Given Nm",
        )
        assert out2.alias_learned is False
    aliases.invalidate_alias_caches()


def test_refresh_embedding_degrades_without_gpu(conn, tmp_path, monkeypatch) -> None:
    from rag import embed

    monkeypatch.delenv("EVAL_LIVE", raising=False)
    monkeypatch.setattr(embed, "CACHE_ROOT", tmp_path)
    # No cached fixture + no EVAL_LIVE → loop degrades, not raises.
    assert refresh_embedding(conn, "doc-1", b"png-bytes") is False


def test_bootstrap_alias_table_counts_the_committed_seed() -> None:
    from cascade.router import ALIAS_TABLE_PATH

    counts = bootstrap_alias_table(ALIAS_TABLE_PATH)
    assert counts["records"] == 86
    assert counts["aliases"] > counts["records"]  # multiple aliases per record
