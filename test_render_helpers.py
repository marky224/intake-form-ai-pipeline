"""Unit tests for non-Chromium helpers in ``synthetic_data.render.render``.

These tests don't launch Playwright, so they run in CI alongside the
schema/parser/signature unit tests. The Chromium-dependent integration
tests live in ``test_render.py`` and ``test_render_batch.py`` (both
slow-marked).
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from synthetic_data.render.render import _extract_field_bboxes, _render_stem, _safe_stem


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


@pytest.mark.parametrize(
    "reserved",
    ["CON", "PRN", "AUX", "NUL", "COM1", "com1", "COM9", "LPT1", "lpt9"],
)
def test_safe_stem_replaces_windows_reserved_names(reserved: str) -> None:
    """Windows reserves these basenames even with an extension —
    ``CON.png`` is reserved the same way ``CON`` is. Fall back to a
    hash so the renderer's PNG/JSON pairs are openable on Windows.
    """
    expected = hashlib.sha256(reserved.encode("utf-8")).hexdigest()[:16]
    assert _safe_stem(reserved) == expected


@pytest.mark.parametrize("trailing_dot", ["foo.", "patient.", "....", "a.b."])
def test_safe_stem_replaces_trailing_dot(trailing_dot: str) -> None:
    """NTFS silently strips trailing dots; falling back to a hash
    prevents two patient_ids differing only by trailing-dot count from
    colliding on Windows.

    Asserts the exact sha256[:16] prefix to lock the contract — a
    looser shape-only check would let a regression change which hash
    the function returns without failing.
    """
    expected = hashlib.sha256(trailing_dot.encode("utf-8")).hexdigest()[:16]
    assert _safe_stem(trailing_dot) == expected


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


def test_extract_field_bboxes_raises_on_duplicate_selector_match() -> None:
    """A duplicate ``data-field`` attribute in the template is a bug —
    surface it via RuntimeError instead of silently bbox-ing the first
    occurrence and losing the rest.

    Mocked Page so the test stays fast and doesn't need Chromium.
    """
    page = MagicMock()
    locator = MagicMock()
    locator.count.return_value = 2
    page.locator.return_value = locator

    with pytest.raises(RuntimeError, match="matched 2 elements"):
        _extract_field_bboxes(page)


def test_extract_field_bboxes_skips_missing_selectors_silently() -> None:
    """count==0 means the template legitimately omits that field —
    silently skip, not raise."""
    page = MagicMock()
    locator = MagicMock()
    locator.count.return_value = 0
    page.locator.return_value = locator

    result = _extract_field_bboxes(page)
    assert result == []
