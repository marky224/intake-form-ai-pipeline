"""Tests for ``cascade.eval_cache`` — the replay-cache machinery.

Tests pin two contracts:

1. ``is_live_mode()`` is read fresh on every call (not at module import).
   ``monkeypatch.setenv`` must be enough to flip the mode mid-test without
   reloading the module.
2. ``cache_path``/``load_cached``/``save_cached`` use the same content-addressable
   ``<provider>/<sha>.json`` layout downstream tests + the renderer rely on.
"""

from __future__ import annotations

import json

import pytest

from cascade import eval_cache

# 64-char lowercase hex fixture. Real shas come from the renderer / ingester
# but any well-formed hex string works for these tests.
_VALID_SHA = "a" * 64
_VALID_SHA_2 = "b" * 64


@pytest.fixture
def isolated_cache_root(tmp_path, monkeypatch):
    """Point CACHE_ROOT at a per-test tmp dir to avoid polluting checked-in fixtures."""
    monkeypatch.setattr(eval_cache, "CACHE_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# is_live_mode
# ---------------------------------------------------------------------------


def test_is_live_mode_defaults_false(monkeypatch):
    """Default — CI never has EVAL_LIVE set."""
    monkeypatch.delenv("EVAL_LIVE", raising=False)
    assert eval_cache.is_live_mode() is False


@pytest.mark.parametrize("truthy", ["true", "TRUE", "True", "1", "yes", "YES", "y", "on"])
def test_is_live_mode_truthy_values(monkeypatch, truthy):
    monkeypatch.setenv("EVAL_LIVE", truthy)
    assert eval_cache.is_live_mode() is True


@pytest.mark.parametrize("falsy", ["false", "0", "no", "n", "off", "", "  ", "maybe"])
def test_is_live_mode_falsy_values(monkeypatch, falsy):
    monkeypatch.setenv("EVAL_LIVE", falsy)
    assert eval_cache.is_live_mode() is False


def test_is_live_mode_handles_whitespace(monkeypatch):
    """Trailing newlines from shell exports shouldn't flip the result."""
    monkeypatch.setenv("EVAL_LIVE", "  true  ")
    assert eval_cache.is_live_mode() is True


def test_is_live_mode_reads_fresh_per_call(monkeypatch):
    """Locked behavior: each call re-reads the env var rather than caching at import."""
    monkeypatch.delenv("EVAL_LIVE", raising=False)
    assert eval_cache.is_live_mode() is False
    monkeypatch.setenv("EVAL_LIVE", "true")
    assert eval_cache.is_live_mode() is True
    monkeypatch.setenv("EVAL_LIVE", "false")
    assert eval_cache.is_live_mode() is False


# ---------------------------------------------------------------------------
# cache_path validation
# ---------------------------------------------------------------------------


def test_cache_path_shape(isolated_cache_root):
    p = eval_cache.cache_path("tier1_paddleocr_local", _VALID_SHA)
    assert p == isolated_cache_root / "tier1_paddleocr_local" / f"{_VALID_SHA}.json"


def test_cache_path_rejects_empty_provider(isolated_cache_root):
    with pytest.raises(ValueError):
        eval_cache.cache_path("", _VALID_SHA)


@pytest.mark.parametrize("bad", ["foo/bar", "..", "tier1\\foo", "tier1/../etc"])
def test_cache_path_rejects_provider_with_slashes(isolated_cache_root, bad):
    """A provider name that escapes the cache dir is a caller bug."""
    with pytest.raises(ValueError):
        eval_cache.cache_path(bad, _VALID_SHA)


@pytest.mark.parametrize(
    "bad_sha",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,  # uppercase not allowed
        "g" * 64,  # non-hex char
        "a" * 63 + "Z",
    ],
)
def test_cache_path_rejects_malformed_sha(isolated_cache_root, bad_sha):
    with pytest.raises(ValueError):
        eval_cache.cache_path("tier1_paddleocr_local", bad_sha)


def test_cache_path_does_not_create_file(isolated_cache_root):
    """cache_path is read-only; it doesn't touch the filesystem."""
    p = eval_cache.cache_path("tier1_paddleocr_local", _VALID_SHA)
    assert not p.exists()
    assert not p.parent.exists()


# ---------------------------------------------------------------------------
# load_cached / save_cached
# ---------------------------------------------------------------------------


def test_load_cached_returns_none_on_miss(isolated_cache_root):
    assert eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA) is None


def test_save_then_load_roundtrip(isolated_cache_root):
    payload = {"queries": [{"alias": "vendor_name", "text": "ACME", "score": 0.95}]}
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA, payload)
    loaded = eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA)
    assert loaded == payload


def test_save_creates_provider_subdir(isolated_cache_root):
    """First save for a new provider creates its subdirectory."""
    sub = isolated_cache_root / "tier3b_claude_bedrock"
    assert not sub.exists()
    eval_cache.save_cached("tier3b_claude_bedrock", _VALID_SHA, {"k": "v"})
    assert sub.is_dir()


def test_save_overwrites_existing_fixture(isolated_cache_root):
    """Re-running EVAL_LIVE intentionally regenerates fixtures."""
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA, {"v": 1})
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA, {"v": 2})
    assert eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA) == {"v": 2}


def test_save_writes_human_diffable_json(isolated_cache_root):
    """Cached fixtures are checked in; reviewer must be able to diff them."""
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA, {"b": 2, "a": 1})
    body = (isolated_cache_root / "tier1_paddleocr_local" / f"{_VALID_SHA}.json").read_text()
    # sort_keys=True → "a" appears before "b" lexicographically
    assert body.index('"a"') < body.index('"b"')
    # indent=2 → multi-line, not jammed onto one line
    assert "\n" in body


def test_separate_provider_subdirs_dont_collide(isolated_cache_root):
    """Same sha under different providers stays distinct (per-tier eval-cache layout)."""
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA, {"tier": 1})
    eval_cache.save_cached("tier2_textract", _VALID_SHA, {"tier": 2})
    assert eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA) == {"tier": 1}
    assert eval_cache.load_cached("tier2_textract", _VALID_SHA) == {"tier": 2}


def test_separate_shas_dont_collide(isolated_cache_root):
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA, {"doc": "a"})
    eval_cache.save_cached("tier1_paddleocr_local", _VALID_SHA_2, {"doc": "b"})
    assert eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA) == {"doc": "a"}
    assert eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA_2) == {"doc": "b"}


def test_load_cached_corrupt_file_raises(isolated_cache_root):
    """Corrupt JSON should surface, not silently return None — caller debugs the cache."""
    p = isolated_cache_root / "tier1_paddleocr_local" / f"{_VALID_SHA}.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        eval_cache.load_cached("tier1_paddleocr_local", _VALID_SHA)
