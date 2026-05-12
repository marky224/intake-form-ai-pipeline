"""Unit tests for non-Chromium helpers in ``synthetic_data.render.render``.

These tests don't launch Playwright, so they run in CI alongside the
schema/parser/signature unit tests. The Chromium-dependent integration
tests live in ``test_render.py`` and ``test_render_batch.py`` (both
slow-marked).
"""

from __future__ import annotations

import hashlib

from synthetic_data.render.render import _render_stem, _safe_stem


def test_safe_stem_passes_through_uuid_unchanged() -> None:
    """Synthea-style UUIDs are already filesystem-safe and pass through."""
    uuid = "aee7bbe1-0c45-c028-1e62-1f4cdb30c273"
    assert _safe_stem(uuid) == uuid


def test_safe_stem_passes_through_safe_ascii() -> None:
    """Alphanumerics + dot/underscore/dash survive sanitization unchanged."""
    assert _safe_stem("patient_abc-123.v2") == "patient_abc-123.v2"


def test_safe_stem_replaces_path_separators() -> None:
    """A patient_id with ``/`` cannot escape the output directory.

    Dots are in the safe whitelist (filenames like ``patient.v2`` are
    legitimate), so ``..`` survives — but ``/`` does not. The resulting
    stem is interpreted as a single filename relative to output_dir,
    so embedded ``..`` text is harmless: ``output_dir/.._.._etc_passwd``
    is a file inside ``output_dir``, not above it.
    """
    assert _safe_stem("../../etc/passwd") == ".._.._etc_passwd"
    assert _safe_stem("a/b/c") == "a_b_c"


def test_safe_stem_replaces_other_unsafe_chars() -> None:
    """Spaces, backslashes, semicolons, quotes — all become underscores."""
    assert _safe_stem("hello world") == "hello_world"
    assert _safe_stem("a\\b") == "a_b"
    assert _safe_stem("evil;rm -rf") == "evil_rm_-rf"
    assert _safe_stem('"quote"') == "_quote_"


def test_safe_stem_handles_dot_only_id() -> None:
    """A dot-only or empty cleaned stem falls back to a hash prefix.

    ``.`` and ``..`` would resolve to the output dir itself or its
    parent on most filesystems — both are unsafe as basenames.
    """
    expected_dot = hashlib.sha256(b".").hexdigest()[:16]
    expected_dotdot = hashlib.sha256(b"..").hexdigest()[:16]
    assert _safe_stem(".") == expected_dot
    assert _safe_stem("..") == expected_dotdot


def test_safe_stem_handles_empty_id() -> None:
    """Empty patient_id falls back to a hash of the empty string."""
    expected = hashlib.sha256(b"").hexdigest()[:16]
    assert _safe_stem("") == expected


def test_safe_stem_handles_all_unsafe_chars() -> None:
    """All-unsafe input collapses to a string of underscores (not a hash fallback)."""
    # All chars get replaced 1-for-1 with `_`, so the result is non-empty
    # and not dot-only, which means the hash fallback does NOT fire.
    assert _safe_stem("///") == "___"


def test_render_stem_appends_disambiguator() -> None:
    """``_render_stem`` adds a stable 8-hex-char sha256 suffix."""
    uuid = "aee7bbe1-0c45-c028-1e62-1f4cdb30c273"
    stem = _render_stem(uuid)
    assert stem.startswith(uuid + "-")
    # Suffix is 8 lowercase hex digits.
    suffix = stem.rsplit("-", 1)[-1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_render_stem_is_deterministic() -> None:
    """Same input → same output across calls (so re-renders are byte-stable)."""
    a = _render_stem("patient-xyz")
    b = _render_stem("patient-xyz")
    assert a == b


def test_render_stem_disambiguates_colliding_sanitized_stems() -> None:
    """Two distinct patient_ids that share a sanitized stem produce
    different render stems via the hash suffix.

    Without the disambiguator, ``a/b`` and ``a_b`` both sanitize to
    ``a_b`` and would silently overwrite each other's outputs in a
    batch run.
    """
    assert _safe_stem("a/b") == _safe_stem("a_b") == "a_b"
    assert _render_stem("a/b") != _render_stem("a_b")
