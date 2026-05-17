"""Tests for ``rag.embed`` — cached/$0 path only (no GPU, no colpali).

CI never has a CUDA device or ``colpali-engine``. These tests pin the
cached-replay contract and that the default path imports neither torch nor
colpali.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from rag import embed


def test_embed_cache_path_rejects_bad_hash() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        embed.embed_cache_path("not-a-hash")


def test_cache_miss_without_eval_live_raises_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVAL_LIVE", raising=False)
    monkeypatch.setattr(embed, "CACHE_ROOT", tmp_path)
    with pytest.raises(embed.EmbeddingUnavailable, match="EVAL_LIVE"):
        embed.embed_image(b"some-png-bytes")
    # The degraded path must not have dragged in the heavy stack.
    assert "torch" not in sys.modules
    assert "colpali_engine" not in sys.modules


def test_cache_hit_returns_committed_fixture(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVAL_LIVE", raising=False)
    monkeypatch.setattr(embed, "CACHE_ROOT", tmp_path)

    png = b"deterministic-png"
    mat = np.random.default_rng(7).standard_normal((11, 128)).astype(np.float32)
    path = embed.embed_cache_path(__import__("hashlib").sha256(png).hexdigest())
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, mat)

    out = embed.embed_image(png)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, mat)
    assert isinstance(path, Path) and path.suffix == ".npy"
