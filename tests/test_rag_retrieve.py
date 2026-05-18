"""Tests for ``rag.retrieve`` — ColBERT MaxSim late-interaction."""

from __future__ import annotations

import numpy as np
import pytest

from rag.retrieve import Neighbor, maxsim, top_k


def test_maxsim_identical_matrices_equals_token_count() -> None:
    """Self-similarity of L2-normalized vectors = 1 per query token."""
    mat = np.random.default_rng(1).standard_normal((5, 16)).astype(np.float32)
    assert maxsim(mat, mat) == pytest.approx(5.0, rel=1e-5)


def test_maxsim_handles_empty_sides() -> None:
    d = np.random.default_rng(3).standard_normal((9, 8)).astype(np.float32)
    assert maxsim(np.zeros((0, 8), dtype=np.float32), d) == 0.0
    assert maxsim(d, np.zeros((0, 8), dtype=np.float32)) == 0.0


def test_top_k_orders_by_score_then_doc_id() -> None:
    q = np.eye(3, dtype=np.float32)
    corpus = {
        "near": np.eye(3, dtype=np.float32),
        "far": np.full((3, 3), -1.0, dtype=np.float32),
        "tie_b": np.eye(3, dtype=np.float32),
        "tie_a": np.eye(3, dtype=np.float32),
    }
    ranked = top_k(q, corpus, k=3)
    assert [n.doc_id for n in ranked] == ["near", "tie_a", "tie_b"]
    assert all(isinstance(n, Neighbor) for n in ranked)
    assert ranked[0].score >= ranked[-1].score


def test_top_k_non_positive_and_empty() -> None:
    q = np.eye(2, dtype=np.float32)
    assert top_k(q, {"a": q}, k=0) == []
    assert top_k(q, {}, k=3) == []
