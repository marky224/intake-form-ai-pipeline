"""Tests for ``rag.store`` — multivector ↔ reserved ``embeddings`` BLOB.

Pins the no-migration promise: the embeddings round-trip through the
*existing* Phase-5 ``embeddings(doc_id, vector BLOB, created_at)`` column
with no schema change.
"""

from __future__ import annotations

import numpy as np
import pytest

from cascade import store
from rag import store as rag_store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def _stub_run(conn, doc_id: str) -> None:
    """Embeddings FK → runs(doc_id); insert the minimal parent row."""
    store.record_run(
        conn,
        doc_id=doc_id,
        vertical="healthcare",
        router_stage=1,
        router_score=5.0,
        final_tier="3a",
        final_confidence=0.5,
        status="review_queue",
        total_latency_ms=1.0,
    )


def test_pack_unpack_roundtrip_preserves_matrix() -> None:
    mat = np.random.default_rng(0).standard_normal((37, 128)).astype(np.float32)
    out = rag_store.unpack_matrix(rag_store.pack_matrix(mat))
    assert out.shape == (37, 128)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, mat, rtol=0, atol=0)


def test_pack_rejects_non_2d() -> None:
    with pytest.raises(ValueError, match="2-D"):
        rag_store.pack_matrix(np.zeros((3, 4, 5), dtype=np.float32))


def test_unpack_rejects_foreign_blob() -> None:
    with pytest.raises(ValueError, match="not a ColQwen embedding"):
        rag_store.unpack_matrix(b"XXXX" + b"\x00" * 16)


def test_upsert_load_roundtrip_through_reserved_column(conn) -> None:
    _stub_run(conn, "doc-a")
    mat = np.arange(12, dtype=np.float32).reshape(3, 4)
    rag_store.upsert_embedding(conn, "doc-a", mat)

    np.testing.assert_array_equal(rag_store.load_embedding(conn, "doc-a"), mat)
    assert rag_store.load_embedding(conn, "missing") is None

    # The column is the reserved one — not a new table / vec0 vtab.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)").fetchall()}
    assert cols == {"doc_id", "vector", "created_at"}


def test_upsert_is_idempotent_overwrite(conn) -> None:
    _stub_run(conn, "doc-a")
    rag_store.upsert_embedding(conn, "doc-a", np.ones((2, 2), dtype=np.float32))
    rag_store.upsert_embedding(conn, "doc-a", np.full((5, 3), 7.0, dtype=np.float32))
    out = rag_store.load_embedding(conn, "doc-a")
    assert out.shape == (5, 3) and float(out[0, 0]) == 7.0


def test_load_corpus_excludes_query_doc(conn) -> None:
    for d in ("doc-a", "doc-b", "doc-c"):
        _stub_run(conn, d)
        rag_store.upsert_embedding(conn, d, np.ones((2, 2), dtype=np.float32))
    corpus = rag_store.load_corpus(conn, exclude=["doc-b"])
    assert set(corpus) == {"doc-a", "doc-c"}
